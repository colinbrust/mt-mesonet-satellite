import datetime as dt
import logging
import re
import tempfile
import time
from operator import itemgetter
from pathlib import Path
from typing import List, Union

import pandas as pd

from .Clean import clean_all
from .Geom import Point
from .Neo4jConn import MesonetSatelliteDB
from .Product import Product
from .Session import Session
from .Task import PendingTaskError, Submit
from .to_db_format import to_db_format

RM_STRINGS = ["_pft", "_std_", "StdDev"]

logging.basicConfig(
    level=logging.INFO,
    filename="/setup/log.txt",
    filemode="w",
    format="%(asctime)s %(message)s",
)

def find_missing_data(conn: MesonetSatelliteDB) -> pd.DataFrame:
    """Looks for the last timestamp for each product and returns the information in a dataframe

    Returns:
        pd.DataFrame: DataFame of dates of the last timestamp of each product.
    """
    dat = conn.get_latest()
    dat = dat.assign(date=dat.date + pd.to_timedelta(1, unit="D"))
    logging.info("Found missing data. ")
    return dat


def start_missing_tasks(
    conn: MesonetSatelliteDB, session: Session, start_now: bool = True
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
            vals = [
                k
                for k, v in p.layers.items()
                if e.lower() in k.lower() and not v.IsQA
            ]
            vals = [n for n in vals if not re.search("|".join(RM_STRINGS), n)]
            layers = layers + vals

        layers = set(layers)

        assert (
            len(sub["date"].drop_duplicates()) == 1
        ), "There are more than two start_dates. You didn't implement anything to deal with this..."

        date = sub["date"][0].to_pydatetime().date()
        today = dt.date.today()

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
    logging.info(
        "New tasks have been launched. Waiting for them to complete..."
    )

    return tasks


def wait_on_tasks(
    tasks: List[Submit],
    session: Session,
    dirname: Union[str, Path],
    wait: int = 300,
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
    time.sleep(wait)
    while True:
        indices = []
        for idx, task in enumerate(tasks):
            try:
                task.download(dirname, session.token, False)
            except PendingTaskError:
                logging.warning(f"{task.task_id} is still running...")
                indices.append(idx)
        try:
            getter = itemgetter(*indices)
            tasks = [*getter(tasks)]
        except TypeError as e:
            logging.info("All tasks have completed.")
            break

        logging.info(f"Waiting {wait} seconds to try again...")


def update_db(dirname: Union[Path, str], conn=MesonetSatelliteDB):

    logging.info("Starting upload to Neo4j DB.")
    cleaned = clean_all(dirname, False)
    formatted = to_db_format(
        f=cleaned, neo4j_pth=None, out_name=None, write=False, split=False
    )
    formatted.reset_index(drop=True, inplace=True)
    conn.post(formatted)
    conn.close()
    logging.info("Upload to Neo4j DB complete.")


def operational_update(conn, session):

    with tempfile.TemporaryDirectory() as dirname:
        tasks = start_missing_tasks(conn=conn, session=session, start_now=True)
        wait_on_tasks(tasks=tasks, session=session, dirname=dirname, wait=3600)
        update_db(dirname=dirname, conn=conn)
