from copy import deepcopy
from datetime import datetime
import sys
import time
import json
import logging
from itertools import islice
from typing import Optional

import rich
import click

from sqeleton.schema import create_schema
from sqeleton.queries.api import current_timestamp

from .dbt import dbt_diff
from .utils import eval_name_template, remove_password_from_url, safezip, match_like
from .diff_tables import Algorithm
from .hashdiff_tables import HashDiffer, DEFAULT_BISECTION_THRESHOLD, DEFAULT_BISECTION_FACTOR
from .joindiff_tables import TABLE_WRITE_LIMIT, JoinDiffer
from .table_segment import TableSegment
from .databases import connect
from .parse_time import parse_time_before, UNITS_STR, ParseError
from .config import apply_config_from_file
from .tracking import disable_tracking, set_entrypoint_name
from .version import __version__


LOG_FORMAT = "[%(asctime)s] %(levelname)s - %(message)s"
DATE_FORMAT = "%H:%M:%S"

COLOR_SCHEME = {
    "+": "green",
    "-": "red",
}

set_entrypoint_name("CLI")


def _remove_passwords_in_dict(d: dict):
    for k, v in d.items():
        if k == "password":
            d[k] = "*" * len(v)
        elif isinstance(v, dict):
            _remove_passwords_in_dict(v)
        elif k.startswith("database"):
            d[k] = remove_password_from_url(v)


def _get_schema(pair):
    db, table_path = pair
    return db.query_table_schema(table_path)


def diff_schemas(table1, table2, schema1, schema2, columns):
    logging.info("Diffing schemas...")
    attrs = "name", "type", "datetime_precision", "numeric_precision", "numeric_scale"
    for c in columns:
        if c is None:  # Skip for convenience
            continue
        diffs = []

        if c not in schema1:
            cols = ", ".join(schema1)
            raise ValueError(f"Column '{c}' not found in table 1, named '{table1}'. Columns: {cols}")
        if c not in schema2:
            cols = ", ".join(schema1)
            raise ValueError(f"Column '{c}' not found in table 2, named '{table2}'. Columns: {cols}")

        col1 = schema1[c]
        col2 = schema2[c]

        for attr, v1, v2 in safezip(attrs, col1, col2):
            if v1 != v2:
                diffs.append(f"{attr}:({v1} != {v2})")
        if diffs:
            logging.warning(f"Schema mismatch in column '{c}': {', '.join(diffs)}")


class MyHelpFormatter(click.HelpFormatter):
    def __init__(self, **kwargs):
        super().__init__(self, **kwargs)
        self.indent_increment = 6

    def write_usage(self, prog: str, args: str = "", prefix: Optional[str] = None) -> None:
        self.write(f"data-diff v{__version__} - efficiently diff rows across database tables.\n\n")
        self.write("Usage:\n")
        self.write(f"  * In-db diff:    {prog} <database_a> <table_a> <table_b> [OPTIONS]\n")
        self.write(f"  * Cross-db diff: {prog} <database_a> <table_a> <database_b> <table_b> [OPTIONS]\n")
        self.write(f"  * Using config:  {prog} --conf PATH [--run NAME] [OPTIONS]\n")


click.Context.formatter_class = MyHelpFormatter


@click.command(no_args_is_help=True)
@click.argument("database1", required=False)
@click.argument("table1", required=False)
@click.argument("database2", required=False)
@click.argument("table2", required=False)
@click.option(
    "-k", "--key-columns", default=[], multiple=True, help="Names of primary key columns. Default='id'.", metavar="NAME"
)
@click.option("-t", "--update-column", default=None, help="Name of updated_at/last_updated column", metavar="NAME")
@click.option(
    "-c",
    "--columns",
    default=[],
    multiple=True,
    help="Names of extra columns to compare."
    "Can be used more than once in the same command. "
    "Accepts a name or a pattern like in SQL. Example: -c col% -c another_col",
    metavar="NAME",
)
@click.option("-l", "--limit", default=None, help="Maximum number of differences to find", metavar="NUM")
@click.option(
    "--bisection-factor",
    default=None,
    help=f"Segments per iteration. Default={DEFAULT_BISECTION_FACTOR}.",
    metavar="NUM",
)
@click.option(
    "--bisection-threshold",
    default=None,
    help=f"Minimal bisection threshold. Below it, data-diff will download the data and compare it locally. Default={DEFAULT_BISECTION_THRESHOLD}.",
    metavar="NUM",
)
@click.option(
    "-m",
    "--materialize-to-table",
    default=None,
    metavar="TABLE_NAME",
    help="(joindiff only) Materialize the diff results into a new table in the database. If a table exists by that name, it will be replaced.",
)
@click.option(
    "--min-age",
    default=None,
    help="Considers only rows older than specified. Useful for specifying replication lag."
    "Example: --min-age=5min ignores rows from the last 5 minutes. "
    f"\nValid units: {UNITS_STR}",
    metavar="AGE",
)
@click.option(
    "--max-age", default=None, help="Considers only rows younger than specified. See --min-age.", metavar="AGE"
)
@click.option("-s", "--stats", is_flag=True, help="Print stats instead of a detailed diff")
@click.option("-d", "--debug", is_flag=True, help="Print debug info")
@click.option("--json", "json_output", is_flag=True, help="Print JSONL output for machine readability")
@click.option("-v", "--verbose", is_flag=True, help="Print extra info")
@click.option("--version", is_flag=True, help="Print version info and exit")
@click.option("-i", "--interactive", is_flag=True, help="Confirm queries, implies --debug")
@click.option("--no-tracking", is_flag=True, help="data-diff sends home anonymous usage data. Use this to disable it.")
@click.option(
    "--case-sensitive",
    is_flag=True,
    help="Column names are treated as case-sensitive. Otherwise, data-diff corrects their case according to schema.",
)
@click.option(
    "--assume-unique-key",
    is_flag=True,
    help="Skip validating the uniqueness of the key column during joindiff, which is costly in non-cloud dbs.",
)
@click.option(
    "--sample-exclusive-rows",
    is_flag=True,
    help="Sample several rows that only appear in one of the tables, but not the other. (joindiff only)",
)
@click.option(
    "--materialize-all-rows",
    is_flag=True,
    help="Materialize every row, even if they are the same, instead of just the differing rows. (joindiff only)",
)
@click.option(
    "--table-write-limit",
    default=TABLE_WRITE_LIMIT,
    help=f"Maximum number of rows to write when creating materialized or sample tables, per thread. Default={TABLE_WRITE_LIMIT}",
    metavar="COUNT",
)
@click.option(
    "-j",
    "--threads",
    default=None,
    help="Number of worker threads to use per database. Default=1. "
    "A higher number will increase performance, but take more capacity from your database. "
    "'serial' guarantees a single-threaded execution of the algorithm (useful for debugging).",
    metavar="COUNT",
)
@click.option(
    "-w",
    "--where",
    default=None,
    help="An additional 'where' expression to restrict the search space. Beware of SQL Injection!",
    metavar="EXPR",
)
@click.option("-a", "--algorithm", default=Algorithm.AUTO.value, type=click.Choice([i.value for i in Algorithm]))
@click.option(
    "--conf",
    default=None,
    help="Path to a configuration.toml file, to provide a default configuration, and a list of possible runs.",
    metavar="PATH",
)
@click.option(
    "--run",
    default=None,
    help="Name of run-configuration to run. If used, CLI arguments for database and table must be omitted.",
    metavar="NAME",
)
@click.option(
    "--dbt",
    is_flag=True,
    help="Run a diff using your local dbt project. Expects to be run from a dbt project folder by default.",
)
@click.option(
    "--cloud",
    is_flag=True,
    help="Add this flag along with --dbt to run a diff using your local dbt project on Datafold cloud. Expects an api key on env var DATAFOLD_API_KEY.",
)
@click.option(
    "--dbt-profiles-dir",
    default=None,
    metavar="PATH",
    help="Override the default dbt profile location (~/.dbt).",
)
@click.option(
    "--dbt-project-dir",
    default=None,
    metavar="PATH",
    help="Override the dbt project directory. Otherwise assumed to be the current directory.",
)
def main(conf, run, **kw):
    if kw["table2"] is None and kw["database2"]:
        # Use the "database table table" form
        kw["table2"] = kw["database2"]
        kw["database2"] = kw["database1"]

    if kw["version"]:
        print(f"v{__version__}")
        return

    if conf:
        kw = apply_config_from_file(conf, run, kw)

    if kw["no_tracking"]:
        disable_tracking()

    if kw.get("interactive"):
        kw["debug"] = True

    if kw["debug"]:
        logging.basicConfig(level=logging.DEBUG, format=LOG_FORMAT, datefmt=DATE_FORMAT)
        if kw.get("__conf__"):
            kw["__conf__"] = deepcopy(kw["__conf__"])
            _remove_passwords_in_dict(kw["__conf__"])
            logging.debug(f"Applied run configuration: {kw['__conf__']}")
    elif kw.get("verbose"):
        logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, datefmt=DATE_FORMAT)
    else:
        logging.basicConfig(level=logging.WARNING, format=LOG_FORMAT, datefmt=DATE_FORMAT)

    try:
        if kw["dbt"]:
            dbt_diff(
                profiles_dir_override=kw["dbt_profiles_dir"],
                project_dir_override=kw["dbt_project_dir"],
                is_cloud=kw["cloud"],
            )
        else:
            return _data_diff(**kw)
    except Exception as e:
        logging.error(e)
        if kw["debug"]:
            raise


def _data_diff(
    database1,
    table1,
    database2,
    table2,
    key_columns,
    update_column,
    columns,
    limit,
    algorithm,
    bisection_factor,
    bisection_threshold,
    min_age,
    max_age,
    stats,
    debug,
    verbose,
    version,
    interactive,
    no_tracking,
    threads,
    case_sensitive,
    json_output,
    where,
    assume_unique_key,
    sample_exclusive_rows,
    materialize_all_rows,
    table_write_limit,
    materialize_to_table,
    dbt,
    cloud,
    dbt_profiles_dir,
    dbt_project_dir,
    threads1=None,
    threads2=None,
    __conf__=None,
):
    if limit and stats:
        logging.error("Cannot specify a limit when using the -s/--stats switch")
        return

    key_columns = key_columns or ("id",)
    bisection_factor = DEFAULT_BISECTION_FACTOR if bisection_factor is None else int(bisection_factor)
    bisection_threshold = DEFAULT_BISECTION_THRESHOLD if bisection_threshold is None else int(bisection_threshold)

    threaded = True
    if threads is None:
        threads = 1
    elif isinstance(threads, str) and threads.lower() == "serial":
        assert not (threads1 or threads2)
        threaded = False
        threads = 1
    else:
        try:
            threads = int(threads)
        except ValueError:
            logging.error("Error: threads must be a number, or 'serial'.")
            return
        if threads < 1:
            logging.error("Error: threads must be >= 1")
            return

    start = time.monotonic()

    if database1 is None or database2 is None:
        logging.error(
            f"Error: Databases not specified. Got {database1} and {database2}. Use --help for more information."
        )
        return

    db1 = connect(database1, threads1 or threads)
    if database1 == database2:
        db2 = db1
    else:
        db2 = connect(database2, threads2 or threads)

    options = dict(
        case_sensitive=case_sensitive,
        where=where,
    )

    if min_age or max_age:
        now: datetime = db1.query(current_timestamp(), datetime)
        now = now.replace(tzinfo=None)
        try:
            if max_age:
                options["min_update"] = parse_time_before(now, max_age)
            if min_age:
                options["max_update"] = parse_time_before(now, min_age)
        except ParseError as e:
            logging.error(f"Error while parsing age expression: {e}")
            return

    dbs = db1, db2

    if interactive:
        for db in dbs:
            db.enable_interactive()

    algorithm = Algorithm(algorithm)
    if algorithm == Algorithm.AUTO:
        algorithm = Algorithm.JOINDIFF if db1 == db2 else Algorithm.HASHDIFF

    if algorithm == Algorithm.JOINDIFF:
        differ = JoinDiffer(
            threaded=threaded,
            max_threadpool_size=threads and threads * 2,
            validate_unique_key=not assume_unique_key,
            sample_exclusive_rows=sample_exclusive_rows,
            materialize_all_rows=materialize_all_rows,
            table_write_limit=table_write_limit,
            materialize_to_table=materialize_to_table
            and db1.parse_table_name(eval_name_template(materialize_to_table)),
        )
    else:
        assert algorithm == Algorithm.HASHDIFF
        differ = HashDiffer(
            bisection_factor=bisection_factor,
            bisection_threshold=bisection_threshold,
            threaded=threaded,
            max_threadpool_size=threads and threads * 2,
        )

    table_names = table1, table2
    table_paths = [db.parse_table_name(t) for db, t in safezip(dbs, table_names)]

    schemas = list(differ._thread_map(_get_schema, safezip(dbs, table_paths)))
    schema1, schema2 = schemas = [
        create_schema(db, table_path, schema, case_sensitive)
        for db, table_path, schema in safezip(dbs, table_paths, schemas)
    ]

    mutual = schema1.keys() & schema2.keys()  # Case-aware, according to case_sensitive
    logging.debug(f"Available mutual columns: {mutual}")

    expanded_columns = set()
    for c in columns:
        cc = c if case_sensitive else c.lower()
        match = set(match_like(cc, mutual))
        if not match:
            m1 = None if any(match_like(cc, schema1.keys())) else f"{db1}/{table1}"
            m2 = None if any(match_like(cc, schema2.keys())) else f"{db2}/{table2}"
            not_matched = ", ".join(m for m in [m1, m2] if m)
            raise ValueError(f"Column '{c}' not found in: {not_matched}")

        expanded_columns |= match

    columns = tuple(expanded_columns - {*key_columns, update_column})

    if db1 == db2:
        diff_schemas(
            table_names[0],
            table_names[1],
            schema1,
            schema2,
            (
                *key_columns,
                update_column,
                *columns,
            ),
        )

    logging.info(f"Diffing using columns: key={key_columns} update={update_column} extra={columns}.")
    logging.info(f"Using algorithm '{algorithm.name.lower()}'.")

    segments = [
        TableSegment(db, table_path, key_columns, update_column, columns, **options)._with_raw_schema(raw_schema)
        for db, table_path, raw_schema in safezip(dbs, table_paths, schemas)
    ]

    diff_iter = differ.diff_tables(*segments)

    if limit:
        assert not stats
        diff_iter = islice(diff_iter, int(limit))

    if stats:
        if json_output:
            rich.print(json.dumps(diff_iter.get_stats_dict()))
        else:
            rich.print(diff_iter.get_stats_string())

    else:
        for op, values in diff_iter:
            color = COLOR_SCHEME[op]

            if json_output:
                jsonl = json.dumps([op, list(values)])
                rich.print(f"[{color}]{jsonl}[/{color}]")
            else:
                text = f"{op} {', '.join(map(str, values))}"
                rich.print(f"[{color}]{text}[/{color}]")

            sys.stdout.flush()

    end = time.monotonic()

    logging.info(f"Duration: {end-start:.2f} seconds.")


if __name__ == "__main__":
    main()
