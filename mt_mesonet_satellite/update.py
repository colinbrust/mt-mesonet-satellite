import pandas as pd
import datetime as dt
from typing import List, Union
from pathlib import Path
import time
from operator import itemgetter
from dotenv import load_dotenv
import os
import re
import tempfile

from .Task import Submit, PendingTaskError
from .Session import Session
from .Geom import Point
from .Clean import clean_all
from .Neo4jConn import MesonetSatelliteDB
from .Product import Product
from .to_db_format import to_db_format


RM_STRINGS = ["_pft", "_std_", "StdDev"]

def find_missing_data(conn: MesonetSatelliteDB) -> pd.DataFrame:
    """Looks for the last timestamp for each product and returns the information in a dataframe

    Returns:
        pd.DataFrame: DataFame of dates of the last timestamp of each product.
    """
    dat = conn.get_latest()
    dat = dat.assign(date = dat.date + pd.to_timedelta(1, unit="D"))
    return dat


def start_missing_tasks(
    conn: MesonetSatelliteDB,  session: Session, start_now: bool = True
) -> List[Submit]:
    """Finds the last data downloaded for each product and starts tasks to fill the missing data.

    Args:
        session (Session): Session object with login credentials.
        master_db (pd.DataFrame): DataFrame with all AppEEARS data.
        start_now (bool, optional): Whether the tasks should be started before being returned. Defaults to True.

    Returns:
        List[Submit]: List of tasks that will fill missing data.
    """
    missing = find_missing_data(conn)
    products = list(set(missing["platform"]))
    products = [Product(x) for x in products]
    geom = Point.from_mesonet()
    tasks = []
    for p in products:
        sub = missing[missing["platform"] == p.product].reset_index(drop=True)
        layers = []
        for e in sub.element.values:
            vals = [k for k, v in p.layers.items() if e.lower() in k.lower() and not v.IsQA]
            vals = [n for n in vals if not re.search('|'.join(RM_STRINGS), n)]
            layers = layers + vals
        
        layers = set(layers)

        assert (
            len(sub["date"].drop_duplicates()) == 1
        ), "There are more than two start_dates. You didn't implement anything to deal with this..."
        
        date = sub["date"][0].to_pydatetime().date()
        today = dt.date.today()
        if (today - date).days == 0:
            print("Old images are less than a week old. Not updating.")
            continue

        start_date = str(date)
        end_date = str(today)
        date = str(date).replace("-", "")
        today = str(today).replace("-", "")
        task_name = f"{p.product}_{date}_{today}"
        task = Submit(
            name=task_name,
            products=[p.product] * len(layers),
            layers=list(layers),
            start_date=start_date,
            end_date=end_date,
            geom=geom,
        )
        tasks.append(task)

    if start_now:
        [task.launch(session.token) for task in tasks]

    return tasks


def wait_on_tasks(
    tasks: List[Submit], session: Session, dirname: Union[str, Path], wait: int = 300
) -> None:
    """Wait until all tasks are completed and download the data once they are.

    Args:
        tasks (List[Submit]): A list of running tasks.
        session (Session): Session object with login credentials.
        dirname (Union[str, Path]): Directory to save results out to.
        wait (int, optional): How long to wait before trying to download data again. Defaults to 300.
    """
    dirname = Path(dirname)

    if not Path(dirname).exists():
        dirname.mkdir(exist_ok=False)

    while True:
        indices = []
        for idx, task in enumerate(tasks):
            try:
                task.download(dirname, session.token, False)
            except PendingTaskError as e:
                print(f"{e}\n{task.task_id} is still running...")
                indices.append(idx)
        try:
            getter = itemgetter(*indices)
            tasks = [*getter(tasks)]
        except TypeError as e:
            print("All tasks have completed.")
            break

        print(f"Waiting {wait} seconds to try again...")
        time.sleep(wait)


def update_db(dirname: Union[Path, str], conn = MesonetSatelliteDB):

    cleaned = clean_all(dirname, False)
    formatted = to_db_format(f=cleaned, neo4j_pth=None, out_name=None, write=False, split=False)
    formatted.reset_index(drop=True, inplace=True)
    conn.post(formatted)
    conn.close()


def operational_update():
    
    load_dotenv()
    conn = MesonetSatelliteDB(
        uri=os.getenv("Neo4jURI"),
        user=os.getenv("Neo4jUser"),
        password=os.getenv("Neo4jPassword")
    )

    session = Session()

    with tempfile.TemporaryDirectory() as dirname:
        tasks = start_missing_tasks(
            conn=conn,
            session=session, 
            start_now=True
        )

        wait_on_tasks(
            tasks=tasks,
            session=session, 
            dirname=dirname, 
            wait=300
        )

        update_db(dirname=dirname, conn=conn)

    session.logout()
    conn.close()