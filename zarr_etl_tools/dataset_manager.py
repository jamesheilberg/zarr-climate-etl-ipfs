# This is necessary for referencing types that aren't fully imported yet. See https://peps.python.org/pep-0563/
from __future__ import annotations

import sys
import logging
import multiprocessing
import multiprocessing.pool
import argparse
import datetime

from .utils.logging import Logging
from .utils.zarr_methods import Publish
from .utils.ipfs import IPFS
from .utils.store import Local, IPLD, S3
from abc import abstractmethod, ABC
from collections.abc import Iterator


class DatasetManager(Logging, Publish, ABC, IPFS):
    """
    This is a base class for data parsers. It is intended to be inherited and implemented by child classes specific to
    each data source.

    It is the base class for any climate data set published in a format that is compatible with being opened in `xarray` and
    transformed into a Zarr. Usable formats so far include netCDF and GRIB2.
    Sets in this category include CHIRPS,CPC,ERA5,VHI,and RTMA.

    For example,for data sourced from CHIRPS,there is a CHIRPS general class that implements most of CHIRPS parsing,
    and further inheriting that class is a fully implemented CHIRPS05 class which updates,parses,and verifies CHIRPS .05 data
    """

    SPAN_HOURLY = "hourly"
    SPAN_DAILY = "daily"
    SPAN_WEEKLY = "weekly"
    SPAN_MONTHLY = "monthly"
    SPAN_YEARLY = "yearly"
    SPAN_SEASONAL = "seasonal"
    DATE_FORMAT_FOLDER = "%Y%m%d"
    DATE_HOURLY_FORMAT_FOLDER = "%Y%m%d%H"
    DATE_FORMAT_METADATA = "%Y/%m/%d"

    def __init__(
        self,
        requested_dask_chunks,
        requested_zarr_chunks,
        requested_ipfs_chunker=None,
        rebuild_requested=False,
        custom_output_path=None,
        custom_latest_hash=None,
        custom_input_path=None,
        console_log=True,
        global_log_level=logging.DEBUG,
        store=None,
        s3_bucket_name=None,
        allow_overwrite=False,
        *args,
        **kwargs,
    ):
        """
        Set member variables to defaults. Setup logging to console and any other requested logs.

        Parameters
        ----------
        rebuild_requested : bool, optional
            Sets `DatasetManager.rebuild_requested`. If this parameter is set, the manager requests and parses all available data from beginning
            to end.
        custom_output_path : str, optional
            Overrides the default path returned by `Convenience.output_path`
        custom_latest_hash : str, optional
            Overrides the default hash lookup defined in `IPFS.latest_hash`
        custom_input_path : str, optional
            A path to use for input files
        console_log : bool, optional
            Enable logging `logging.INFO` level and higher statements to console. For more customization, see `DatasetManager.log_to_console`
        global_log_level : str, optional
            The root logger `logger.getLogger()` will be set to this level. Recommended to be `logging.DEBUG`, so all logging
            statements will be generated and then logging handlers can decide what to do with them.
        store : str | None
            A string indicating the type of filestore to use (one of, "local", "ipld" or "s3"). A corresponding store object will be initialized.
            If `None`, the store is left unset and the default store interface defined in `Attributes.store` (local) is returned when the property is
            accessed. If using S3, the environment variables `AWS_ACCESS_KEY_ID`and `AWS_SECRET_ACCESS_KEY` must be specified
            in the ~/.aws/credentials file or set manually.
        s3_bucket_name : str
            Name of the S3 bucket where this dataset's Zarrs are stored. Only used if "s3" store is used.
        allow_overwrite : bool
            Unless this is set to `True`, inserting or overwriting data for dates before the dataset's current end date will fail with a
            warning message.
        """
        # call IPFS init
        super().__init__()
        # Set member variable defaults
        self.new_files = []
        self.custom_output_path = custom_output_path
        self.custom_latest_hash = custom_latest_hash
        self.custom_input_path = custom_input_path
        self.rebuild_requested = rebuild_requested

        # Create a store object based on the passed store string. If `None`, treat as "local". If any string other than "local", "ipld", or "s3" is
        # passed, raise a `ValueError`.
        if store is None or store == "local":
            self.store = Local(self)
        elif store == "ipld":
            self.store = IPLD(self)
        elif store == "s3":
            self.store = S3(self, s3_bucket_name)
        else:
            raise ValueError("Store must be one of 'local', 'ipld', or 's3'")

        # Assign the allow overwrite flag. The value should always be either `True` or `False`.
        # Always allow overwrites if IPLD for backwards compatibility
        self.overwrite_allowed = allow_overwrite or isinstance(self.store, IPLD)

        # Print log statements to console by default
        if console_log:
            self.log_to_console()

        # Set the logging level of logger.getLogger(), which is the logging module's root logger and will control the level of log statements
        # that are enabled globally. If this is set to `logging.DEBUG`, all log statements will be enabled by default and will be forwarded to
        # handlers set by either `logging.Logger.addHandler`, `DatasetManager.log_to_file`, or `DatasetManager.log_to_console`.
        logging.getLogger().setLevel(global_log_level)

        # Add a custom exception handler that will print the traceback to loggers
        sys.excepthook = self.log_except_hook

        # set chunk sizes (usually specified in the ETL manager class init)
        self.requested_dask_chunks = requested_dask_chunks
        self.requested_zarr_chunks = requested_zarr_chunks
        self.requested_ipfs_chunker = requested_ipfs_chunker

        # Dask distributed configuration defaults, mostly related to memory usage
        self.dask_scheduler_worker_saturation = 1.2
        self.dask_worker_mem_target = 0.65
        self.dask_worker_mem_spill = 0.65
        self.dask_worker_mem_pause = 0.92
        self.dask_worker_mem_terminate = 0.98

        # Each thread will use a CPU if self.dask_num_workers is 1. Setting it to use 75% of available CPUs seems to be reasonable.
        # More specifically, setting it to use 24 of 32 CPUs on a 256 RAM server worked well for ERA5-Land. However, in that case,
        # the choice of number of CPUs was actually a factor of RAM size.
        self.dask_num_threads = max(1, int(multiprocessing.cpu_count() * 0.75))

        # Usually set to 1 to avoid data transfer between workers
        self.dask_num_workers = 1

    # SETUP

    def __str__(self) -> str:
        """
        Returns
        -------
        str
            The name of the dataset
        """
        return self.name()

    def __eq__(self, other: DatasetManager) -> bool:
        """
        All instances of this class will compare equal to each other.

        Returns
        -------
        bool
            If the other `DatasetManager` instance has the same name, return `True`
        """
        return str(self) == other

    def __hash__(self):
        return hash(str(self))

    # MINIMUM ETL METHODS

    @abstractmethod
    def static_metadata(self):
        """
        Placeholder indicating necessity of instantiating static metadata at the top of an ETL manager script
        """
        ...

    @abstractmethod
    def update_local_input(self):
        """
        Check for updates to local input files (usually by checking a remote location where climate data publishers post updated
        data)
        """
        self.new_files = []

    @abstractmethod
    def prepare_input_files(self, keep_originals: bool = True):
        """
        Convert each of the input files (and associated metadata files) to a collection of daily netCDF4 classic files suitable for
        reading by Kerchunk and intake into Xarray. This allows us to stack data into modern, performant N-Dimensional Zarr data.

        Parameters
        ----------

        keep_originals : bool, optional
            An optional flag to preserve the original files for debugging purposes. Defaults to True.
        """
        pass

    def populate_metadata(self):
        """
        Fill the metadata with values describing this set, using the static_metadata as a base template.
        """
        if hasattr(self, "metadata") and self.metadata is not None:
            self.metadata = self.metadata.update(self.static_metadata)
        else:
            self.metadata = self.static_metadata

    def set_zarr_metadata(self, dataset):
        """
        Placeholder indicating necessity of possibly editing Zarr metadata within an ETL manager script
        Method to align Zarr metadata with requirements of Zarr exports and STAC metadata format
        Happens after `populate_metadata` and immediately before data publication.
        """
        return super().set_zarr_metadata(dataset)

    # ETL GENERATION FUNCTIONS

    @classmethod
    def get_subclasses(cls) -> Iterator:
        """Create a generator with all the subclasses and sub-subclasses of a parent class"""
        for subclass in cls.__subclasses__():
            yield from subclass.get_subclasses()
            yield subclass

    def get_dataset_manager_from_name(self, name: str) -> type:
        """
        Method to return the subclass instance corresponding to the name provided when invoking the ETL

        Parameters
        ----------
        name : str
            The str returned by the name() property of the dataset to be parsed

        Returns
        -------
        type
            A dataset source class

        """
        for source in self.get_subclasses():
            if (
                "gfs" in name
            ):  # special handling of GFS which generates multiple keys in a single class
                if source.json_key() == name:
                    return source
            else:
                if source.name() == name:
                    return source
        print(
            f"failed to set manager from name {name}, could not find corresponding class"
        )

    def parse_command_line(self) -> tuple[type | dict]:
        """
        When this file is called as a script, this function will run automatically, reading input arguments and flags from the
        command line

        Returns
        -------
        tuple[ type | dict]
            A tuple of a dataset source class and a dictionary of command line arguments to be used by `run_etl`

        """
        parser = self.command_line_parser()
        # use argparse to parse submitted CLI options
        arguments = parser.parse_args()
        # this replaces the passed string for each source with a set manager instance
        arguments = vars(arguments)
        return arguments

    def command_line_parser(self) -> argparse.ArgumentParser:
        """
        Build a parser and populate it with the argument defaults described in command_line_args

        Returns
        -------
        parser | argparse.ArgumentParser
            An ArgumentParser populated with valid command line flags for generating ETLs

        """
        parser = argparse.ArgumentParser(
            formatter_class=argparse.ArgumentDefaultsHelpFormatter
        )

        for argument, arg_opts in self.command_line_args.items():
            parser.add_argument(argument, **arg_opts)

        return parser

    @property
    def command_line_args(self) -> dict:
        """
        Command line arguments for generate + their options and default values

        Returns
        -------
        command_line_args | dict
            A dictionary of command line arguments and their corresponding options

        """
        command_line_args = {
            "source": {
                "help": "a valid source key. Script will fail if an invalid string is passed"
            },
            "store": {
                "help": "a valid store key. Accepts 's3', 'ipld', or 'local'. Script will fail if invalid string is passed"
            },
            "--s3-bucket": {
                "help": "Name of the S3 bucket where this dataset's Zarrs are stored. Only used if 's3' store is used. Defaults to None"
            },
            "--rebuild": {
                "action": "store_true",
                "help": "rebuild from beginning of history and generate a new CID independent of any existing data",
            },
            "--date-range": {
                "nargs": 2,
                "metavar": "YYYY-MM-DD",
                "type": datetime.datetime.fromisoformat,
                "help": "if supported by any of the specified sets,you can specify a range of dates to parse instead of the entire set",
            },
            "--latitude-range": {
                "nargs": 2,
                "metavar": ("MIN", "MAX"),
                "type": float,
                "help": "if supported by any specified source,you can pass a latitude range to parse instead of the entire set",
            },
            "--longitude-range": {
                "nargs": 2,
                "metavar": ("MIN", "MAX"),
                "type": float,
                "help": "if supported by any specified source,you can pass a longitude range to parse instead of the entire set",
            },
            "--only-parse": {
                "action": "store_true",
                "help": "only run a parse,using locally availabe data",
            },
            "--only-metadata": {
                "action": "store_true",
                "help": "only update metadata,using data available on IPFS",
            },
            "--only-update-input": {
                "action": "store_true",
                "help": "only run the update local input function",
            },
            "--only-prepare-input": {
                "action": "store_true",
                "help": "Instead of running the full parse,just run the dataset manager's prepare_input_files and create_zarr_json methods.\
                                This will also run the update input function unless --only-parse has been specified as well.",
            },
            "--local-output": {
                "action": "store_true",
                "help": "write output Zarr to disk instead of IPFS",
            },
            "--custom-output-path": {
                "help": "override the class's automatic output path generation"
            },
            "--custom-head-metadata": {
                "help": "override the class's automatic head lookup"
            },
            "--custom-latest-hash": {
                "help": "override the class's automatic latest hash lookup"
            },
            "--era5-enable-caching": {
                "action": "store_true",
                "help": "allow requests for cached files on ERA5",
            },
            "--era5-skip-finalization": {
                "action": "store_true",
                "help": "skip finalization check and overwriting",
            },
        }
        return command_line_args

    def run_etl(
        self,
        dataset_name: str,
        store: str,
        s3_bucket_name: str = None,
        date_range: list[datetime.datetime, datetime.datetime] = None,
        rebuild: bool = False,
        only_parse: bool = False,
        only_update_input: bool = False,
        only_prepare_input: bool = False,
        only_metadata: bool = False,
        custom_output_path: str = None,
        custom_latest_hash: str = None,
        *args,
        **kwargs,
    ):
        """
        Perform all the ETL steps requested by the combination of flags passed. Retrieve original published data by
        checking remote locations for updates, parse it into Arbol's format, and add it to IPFS.

        By default, this will run a full ETL on the dataset whose `name` corresponds to `dataset_name`,
        meaning it will update input, parse input, and store the parsed output on the specified storage medium.

        Read the code for `commmand_line_args` to understand how these kwargs are instantiated on the command line.

        Parameters
        ----------
        dataset_name : str
            The name() property of the dataset to be parsed

        store : str
            The store type of the dataset to be parsed. Accepts 's3', 'ipld', or 'local'.

        s3_bucket_name : str
            Name of the S3 bucket where this dataset's Zarrs are stored. Only used if "s3" store is used. Defaults to None

        date_range : list[datetime.datetime, datetime.datetime], optional
            A date range within which to download and parse data. Defaults to None.

        rebuild : bool, optional
            A boolean to fully rebuild the dataset, regardless of its current status. Defaults to False.

        only_parse : bool, optional
            A boolean to skip updating local data and only parse the data. Defaults to False.

        only_update_input : bool, optional
            A boolean to skip parsing data and only update local files. Defaults to False.

        only_prepare_input : bool, optional
            A boolean to skip updating and parsing data and only prepare the local Zarr JSON. Defaults to False.

        only_metadata : bool, optional
            A boolean to only update a dataset's STAC metadata. Defaults to False.

        custom_output_path : str, optional
            A str indicating a custom local destination for a Zarr being output locally. Defaults to None.

        custom_head_metadata : str, optional
            A str hash pointing to a custom head for the metadata, instead of the latest corresponding STAC Item. Defaults to None.

        custom_latest_hash : str, optional
            A str hash pointing to a custom iteration of the dataset, instead of the latest corresponding hash. Defaults to None.

        """
        # Find the dataset class (e.g. CHIRPSPrelim05) from its name string
        dataset_class = self.get_dataset_manager_from_name(dataset_name)
        # Initialize a manager for the given class. For example,if class is ERA5Precip, the manager will be ERA5Precip([args]). This will create
        # INFO and DEBUG logs in the current working directory.
        manager = dataset_class(
            store=store,
            s3_bucket_name=s3_bucket_name,
            custom_output_path=custom_output_path,
            custom_latest_hash=custom_latest_hash,
            rebuild=rebuild,
        )
        # Initialize logging for the ETL
        manager.log_to_file()
        manager.log_to_file(level=logging.DEBUG)
        # Set parse to False by default, unless user specifies `only_parse`. This will be changed to True if new files found by update_local_input
        trigger_parse = only_parse
        # update local files
        if only_parse:
            manager.info(
                "only parse flag present,skipping update of local input and using locally available data"
            )
        elif only_metadata:
            manager.info(
                "only metadata flag present,skipping update of local input and parse to update metadata using the existing Zarr on IPFS"
            )
        else:
            manager.info("updating local input")
            # update_local_input will return True if parse should be triggered
            trigger_parse = manager.update_local_input(
                rebuild=rebuild, date_range=date_range
            )
            if only_update_input:
                # we're finished if only update input was set
                manager.info("ending here because only update local input flag is set")
                return
        # only update metadata and/or prepare input if these flags are specified
        if only_metadata:
            manager.info(f"preparing metadata for {manager}")
            manager.only_update_metadata()
            manager.info(f"Metadata for {manager} successfully updated")
        if only_prepare_input:
            manager.info(
                "Only prepare input requested, just preparing source files for parsing and creating corresponding Zarr JSON file"
            )
            manager.prepare_input_files()
            manager.create_zarr_json()
        # parse if only_parse flag is set or if parse was triggered by local input update return value
        if trigger_parse:
            manager.info(f"parsing {manager}")
            # parse will return `True` if new data was parsed
            if manager.parse():
                manager.info(f"Data for {manager} successfully parsed")
            else:
                manager.info("no new data parsed, ending here")
        else:
            manager.info("no new data detected and parse not set to force, ending here")

    def run_etl_as_script(self):
        """
        Run an ETL over the command line by invoking its name and any kwargs.
        All possible kwargs described under `parse_command_line`.
        Place this function in the '__main__' section of ETL manager scripts so they can be independently invoked
        """
        # Get generation args and flags from the command line
        generate_kwargs = self.parse_command_line()
        dataset_name = generate_kwargs["source"]
        generate_kwargs.pop("source")  # exclude the original source argument
        # Pass the command line args to `generate`
        self.run_etl(dataset_name, **generate_kwargs)
