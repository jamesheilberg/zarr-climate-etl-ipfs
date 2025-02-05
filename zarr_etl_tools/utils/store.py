# This is necessary for referencing types that aren't fully imported yet. See https://peps.python.org/pep-0563/
from __future__ import annotations

import os
import s3fs
import xarray as xr
import ipldstore
import pathlib
import fsspec
import collections
from .. import dataset_manager
from abc import abstractmethod, ABC


class StoreInterface(ABC):
    """
    Base class for an interface that can be used to access a dataset's Zarr.

    Zarrs can be stored in different types of data stores, for example IPLD, S3, and the local filesystem, each of which is accessed slightly
    differently in Python. This class abstracts the access to the underlying data store by providing functions that access the Zarr on the store
    in a uniform way, regardless of which is being used.
    """

    def __init__(self, dm: dataset_manager.DatasetManager):
        """
        Create a new `StoreInterface`. Pass the dataset manager this store is being associated with, so the interface will have access to
        dataset properties.

        Parameters
        ----------
        dm : dataset_manager.DatasetManager
            The dataset to be read or written.
        """
        self.dm = dm

    @abstractmethod
    def mapper(self, **kwargs: dict) -> collections.abc.MutableMapping:
        """
        Parameters
        ----------
        **kwargs : dict
            Implementation specific keywords

        Returns
        -------
        collections.abc.MutableMapping
            A key/value mapping of files to contents
        """
        pass

    @property
    @abstractmethod
    def has_existing(self) -> bool:
        """
        Returns
        -------
        bool
            Return `True` if there is existing data for this dataset on the store.
        """
        pass

    def dataset(self, **kwargs: dict) -> xr.Dataset | None:
        """
        Parameters
        ----------
        **kwargs
            Implementation specific keyword arguments to forward to `StoreInterface.mapper`. S3 and Local accept `refresh`, and IPLD accepts `set_root`.

        Returns
        -------
        xr.Dataset | None
            The dataset opened in xarray or None if there is no dataset currently stored.
        """
        if self.has_existing:
            return xr.open_zarr(self.mapper(**kwargs))
        else:
            return None


class S3(StoreInterface):
    """
    Provides an interface for reading and writing a dataset's Zarr on S3.

    To connect to a Zarr on S3 (i.e., at "s3://[bucket]/[dataset_json_key].zarr"), create a new S3 object using a `dataset_manager.DatasetManager` object
    and bucket name, and define both `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` in the ~/.aws/credentials file or shell environment.

    After initialization, use the member functions to access the Zarr. For example, call `S3.mapper` to get a `MutableMapping` that can be passed to
    `xarray.open_zarr` and `xarray.to_zarr`.
    """

    def __init__(self, dm: dataset_manager.DatasetManager, bucket: str):
        """
        Get an interface to a dataset's Zarr on S3 in the specified bucket.

        Parameters
        ----------
        dm : dataset_manager.DatasetManager
            The dataset to be read or written.
        bucket : str
            The name of the S3 bucket to connect to (s3://[bucket])
        """
        super().__init__(dm)
        if not bucket:
            raise ValueError("Must provide bucket name if parsing to S3")
        self.bucket = bucket

    def fs(self, refresh: bool = False) -> s3fs.S3FileSystem:
        """
        Get an `s3fs.S3FileSystem` object by logging in with the AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY environment variables (which ideally should
        be set beforehand in the ~/.aws/credentials file). By default, the filesystem is only created once, the first time this function is called. To force it create a
        new one, set `refresh` to `True`.

        Parameters
        ----------
        refresh : bool
            If set to `True`, a new `s3fs.S3FileSystem` will be created even if this object has one already

        Returns
        -------
        s3fs.S3FileSystem
            A filesystem object for interfacing with S3
        """
        if refresh or not hasattr(self, "_fs"):
            try:
                self._fs = s3fs.S3FileSystem(
                    key=os.environ["AWS_ACCESS_KEY_ID"],
                    secret=os.environ["AWS_SECRET_ACCESS_KEY"]
                    )
            except KeyError:  # KeyError indicates credentials have not been manually specified
                self._fs = s3fs.S3FileSystem()  # credentials automatically supplied from ~/.aws/credentials
            self.dm.info("Connected to S3 filesystem")
        return self._fs

    @property
    def url(self) -> str:
        """
        Get the S3-protocol URL to the parent `DatasetManager`'s Zarr .

        Returns
        -------
        str
            A URL string starting with "s3://" followed by the path to the Zarr.
        """
        return f"s3://{self.bucket}/datasets/{self.dm.json_key()}.zarr"

    def __str__(self) -> str:
        return self.url

    def mapper(self, refresh: bool = False, **kwargs: dict) -> fsspec.mapping.FSMap:
        """
        Get a `MutableMapping` representing the S3 key/value store. By default, the mapper will be created only once, when this function is first
        called. To force a new mapper, set `refresh` to `True`.

        Parameters
        ----------
        refresh : bool
            Set to `True` to force a new mapper to be created even if this object has one already
        **kwargs : dict
            Arbitrary keyword args supported for compatibility with IPLD.

        Returns
        -------
        s3fs.S3Map
            A `MutableMapping` which is the S3 key/value store
        """
        if refresh or not hasattr(self, "_mapper"):
            self._mapper = s3fs.S3Map(root=self.url, s3=self.fs())
        return self._mapper

    @property
    def has_existing(self) -> bool:
        """
        Returns
        -------
        bool
            Return `True` if there is a Zarr at `S3.url`
        """
        return self.fs().exists(self.url)


class IPLD(StoreInterface):
    """
    Provides an interface for reading and writing a dataset's Zarr on IPLD.

    If there is existing data for the dataset, it is assumed to be stored at the hash returned by `IPLD.dm.latest_hash`, and the mapper will
    return a hash that can be used to retrieve the data. If there is no existing data, or the mapper is called without `set_root`, an unrooted
    IPFS mapper will be returned that can be used to write new data to IPFS and generate a new recursive hash.
    """

    def mapper(self, set_root: bool = True, **kwargs: dict) -> ipldstore.IPLDStore:
        """
        Get an IPLD mapper by delegating to `ipldstore.get_ipfs_mapper`, passing along an IPFS chunker value if the associated dataset's
        `requested_ipfs_chunker` property has been set.

        If `set_root` is `False`, the root will not be set to the latest hash, so the mapper can be used to open a new Zarr on the IPLD
        datastore. Otherwise, `DatasetManager.latest_hash` will be used to get the latest hash (which is stored in the STAC at the IPNS key
        for the dataset).

        Parameters
        ----------
        set_root : bool
            Return a mapper rooted at the dataset's latest hash if `True`, otherwise return a new mapper.
        **kwargs
            Arbitrary keyword args supported for compatibility with S3 and Local.

        Returns
        -------
        ipldstore.IPLDStore
            An IPLD `MutableMapping`, usable, for example, to open a Zarr with `xr.open_zarr`
        """
        if self.dm.requested_ipfs_chunker:
            mapper = ipldstore.get_ipfs_mapper(chunker=self.dm.requested_ipfs_chunker)
        else:
            mapper = ipldstore.get_ipfs_mapper()
        self.dm.info(f"IPFS chunker is {mapper._store._chunker}")
        if set_root and self.dm.latest_hash():
            mapper.set_root(self.dm.latest_hash())
        return mapper

    def __str__(self) -> str:
        """
        Returns
        -------
        str
            The path as "/ipfs/[hash]". If the hash has not been determined, just return "/ipfs/".
        """
        if not self.dm.latest_hash():
            return "/ipfs/"
        else:
            return f"/ipfs/{self.dm.latest_hash()}"

    @property
    def has_existing(self) -> bool:
        """
        Returns
        -------
        bool
            Return `True` if the dataset has a latest hash, or `False` otherwise.
        """
        return bool(self.dm.latest_hash())


class Local(StoreInterface):
    """
    Provides an interface for reading and writing a dataset's Zarr on the local filesystem.

    The path of the Zarr is assumed to be the return value of `Local.dm.output_path`. That is the path used automatically under normal conditions, so this
    class doesn't provide a way to use any other path.
    """

    def fs(self, refresh: bool = False) -> fsspec.implementations.local.LocalFileSystem:
        """
        Get an `fsspec.implementations.local.LocalFileSystem` object. By default, the filesystem is only created once, the first time this function is
        called. To force it create a new one, set `refresh` to `True`.

        Parameters
        ----------
        refresh : bool
            If set to `True`, a new `fsspec.implementations.local.LocalFileSystem` will be created even if this object has one already

        Returns
        -------
        fsspec.implementations.local.LocalFileSystem
            A filesystem object for interfacing with the local filesystem
        """
        if refresh or not hasattr(self, "_fs"):
            self._fs = fsspec.filesystem("file")
        return self._fs

    def mapper(self, refresh=False, **kwargs) -> fsspec.mapping.FSMap:
        """
        Get a `MutableMapping` representing a local filesystem key/value store.
        By default, the mapper will be created only once, when this function is first
        called. To force a new mapper, set `refresh` to `True`.

        Parameters
        ----------
        refresh : bool
            Set to `True` to force a new mapper to be created even if this object has one already.
        **kwargs : dict
            Arbitrary keyword args supported for compatibility with IPLD.

        Returns
        -------
        fsspec.mapping.FSMap
            A `MutableMapping` which is a key/value representation of the local filesystem
        """
        if refresh or not hasattr(self, "_mapper"):
            self._mapper = self.fs().get_mapper(self.path)
        return self._mapper

    def __str__(self) -> str:
        return str(self.path)

    @property
    def path(self) -> pathlib.Path:
        """
        Returns
        -------
        pathlib.Path
            Path to the Zarr on the local filesystem
        """
        return self.dm.output_path().joinpath(f"{self.dm.name()}.zarr")

    @property
    def has_existing(self) -> bool:
        """
        Returns
        -------
        bool
            Return `True` if there is a local Zarr for this dataset, `False` otherwise.
        """
        return self.path.exists()
