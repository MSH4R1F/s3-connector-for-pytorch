#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  // SPDX-License-Identifier: BSD

import io
import logging
import os
import pickle
import queue
import urllib.parse
from contextlib import contextmanager
from pathlib import Path
from typing import Generator, Union, Optional, cast
from typing import List
from torch import Future
from torch.distributed.checkpoint.metadata import Metadata, StorageMeta
from torch.distributed.checkpoint.storage import ( WriteResult)
from torch.distributed.checkpoint.filesystem import _split_by_size_and_type

from torch.distributed.checkpoint.planner import (
    SavePlan,
    SavePlanner,
    LoadPlan,
    LoadPlanner
)
import torch.distributed as dist
from s3torchconnectorclient._mountpoint_s3_client import S3Exception
from tenacity import (
    retry,
    stop_after_attempt,
    retry_if_exception_type,
    before_sleep_log,
    after_log,
    wait_random_exponential,
)
from torch.distributed.checkpoint.filesystem import (
    FileSystemReader,
    FileSystemWriter,
    FileSystemBase,
)
import torch
import sys

from s3torchconnector._s3client import S3Client
from s3torchconnector._s3dataset_common import parse_s3_uri
from .. import S3ClientConfig
from .s3_prefix_strategy import S3PrefixStrategyBase, DefaultPrefixStrategy
from .._user_agent import UserAgent

logger = logging.getLogger(__name__)
logging.basicConfig(
    stream=sys.stdout,
    format="%(levelname)s %(name)s %(asctime)-15s %(filename)s:%(lineno)d %(message)s",
)
_metadata_fn: str = ".metadata"

logging.getLogger().setLevel(logging.DEBUG)
DEFAULT_SUFFIX = ".distcp"

class S3FileSystem(FileSystemBase):
    def __init__(
        self,
        region: str,
        s3_client: Optional[S3Client] = None,
        s3client_config: Optional[S3ClientConfig] = None,
    ) -> None:
        self._path: Union[str, os.PathLike] = ""
        user_agent = UserAgent(["dcp", torch.__version__])
        self._client = (
            S3Client(
                region=region, user_agent=user_agent, s3client_config=s3client_config
            )
            if s3_client is None
            else s3_client
        )

    @contextmanager
    def create_stream(
        self, path: Union[str, os.PathLike], mode: str
    ) -> Generator[io.IOBase, None, None]:
        """
        Create a stream for reading or writing to S3.

        Args:
            path (Union[str, os.PathLike]): The S3 path to read or write.
            mode (str): The mode for the stream. Supports 'rb' for read mode and 'wb' for write mode.

        Yields:
            io.BufferedIOBase: A stream for reading or writing to S3.

        Raises:
            ValueError: If the mode is not 'rb' or 'wb'.
        """
        path_str = _path_or_str_to_str(path)
        bucket, key = parse_s3_uri(path_str)

        if mode == "wb":  # write mode
            logger.debug("create_stream writable for %s", path_str)
            with self._client.put_object(bucket, key) as stream:
                yield stream
        elif mode == "rb":  # read mode
            logger.debug("create_stream readable for %s", path_str)
            with self._client.get_object(bucket, key) as stream:
                yield stream
        else:
            raise ValueError(
                f"Invalid {mode=} mode argument: create_stream only supports rb (read mode) & wb (write mode)"
            )

    def concat_path(self, path: Union[str, os.PathLike], suffix: str) -> str:
        """
        Concatenate a suffix to the given path.

        Args:
            path (Union[str, os.PathLike]): The base path.
            suffix (str): The suffix to concatenate.

        Returns:
            str: The concatenated path.
        """
        logger.debug("concat paths %s and %s", path, suffix)
        path_str = os.fspath(path)
        result = os.path.join(path_str, suffix)
        return result

    def init_path(self, path: Union[str, os.PathLike]) -> Union[str, os.PathLike]:
        """
        Initialize the path for the filesystem.

        Args:
            path (Union[str, os.PathLike]): The path to initialize.

        Returns:
            Union[str, os.PathLike]: The initialized path.
        """
        logger.debug("init_path for %s", path)
        self._path = path
        return self._path

    def rename(
        self, old_path: Union[str, os.PathLike], new_path: Union[str, os.PathLike]
    ) -> None:
        """Rename an object in S3.

        This is emulated by copying it to a new path and deleting the old path. The deletion part is retried (see also
        :func:`S3FileSystem._delete_with_retry`).

        Args:
            old_path (Union[str, os.PathLike]): The current path of the object.
            new_path (Union[str, os.PathLike]): The new path for the object.

        Raises:
            ValueError: If the old and new paths point to different buckets.
            S3Exception: If there is an error with the S3 client.
        """
        logger.debug("rename %s to %s", old_path, new_path)

        old_path_str = _path_or_str_to_str(old_path)
        new_path_str = _path_or_str_to_str(new_path)

        old_bucket, old_key = parse_s3_uri(old_path_str)
        escaped_old_key = self._escape_path(old_key)
        logger.debug("rename: escaped version of the source key: %s", escaped_old_key)
        new_bucket, new_key = parse_s3_uri(new_path_str)

        if old_bucket != new_bucket:
            raise ValueError(
                f"Source and destination buckets cannot be different (rename does not support cross-buckets operations)"
            )

        self._client.copy_object(
            src_bucket=old_bucket,
            src_key=escaped_old_key,
            dst_bucket=new_bucket,
            dst_key=new_key,
        )
        logger.debug("rename: copied %s to %s successfully", old_path_str, new_path_str)
        self._delete_with_retry(old_bucket, old_key)
        logger.debug("rename: s3://%s/%s successfully", old_bucket, old_key)

    def mkdir(self, path: Union[str, os.PathLike]) -> None:
        """No-op method for creating directories in S3 (not needed)."""
        pass

    def exists(self, path: Union[str, os.PathLike]) -> bool:
        logger.debug("exists %s", path)

        path_str = _path_or_str_to_str(path)
        bucket, key = parse_s3_uri(path_str)
        try:
            self._client.head_object(bucket, key)
        except S3Exception as e:
            if str(e) != "Service error: The object was not found":
                raise
            return False
        return True

    def rm_file(self, path: Union[str, os.PathLike]) -> None:
        logger.debug("remove %s", path)

        path_str = _path_or_str_to_str(path)
        bucket, key = parse_s3_uri(path_str)
        try:
            self._client.delete_object(bucket, key)
        except S3Exception:
            logger.exception("Failed to remove object from S3")

    @classmethod
    def validate_checkpoint_id(cls, checkpoint_id: Union[str, os.PathLike]) -> bool:
        logger.debug("validate_checkpoint_id for %s", checkpoint_id)

        if isinstance(checkpoint_id, Path):
            return True

        try:
            parse_s3_uri(_path_or_str_to_str(checkpoint_id))
        except ValueError:
            return False
        return True

    @retry(
        retry=retry_if_exception_type(S3Exception),
        stop=stop_after_attempt(3),
        wait=wait_random_exponential(multiplier=1, max=5),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        after=after_log(logger, logging.ERROR),
        reraise=True,
    )
    def _delete_with_retry(self, bucket_name: str, old_key: str):
        """Wrapper around :func:`S3Client.delete_object` to retry the deletion.

        Will retry a maximum of 3 times, only for `S3Exception`s, and wait between retries. It will reraise the caught
        exception too, and logs retries and final error, if any."""
        self._client.delete_object(bucket_name, old_key)

    @staticmethod
    def _escape_path(string):
        """URL-encodes path segments while preserving '/' separators using urllib.parse.quote().

        Args:
            string (str): URL path string to escape

        Returns:
            str: Path string with each segment percent-encoded, separators preserved
        """
        if not string:
            return string
        parts = []
        for part in string.split("/"):
            parts.append(urllib.parse.quote(part, safe=""))
        return "/".join(parts)


from torch.distributed.checkpoint.planner import SavePlan
import dataclasses
from dataclasses import dataclass, field


@dataclass
class StorageMetadata:
    """Metadata for S3 storage prefix."""
    prefix: str

class S3StorageWriter(FileSystemWriter):
    def __init__(
        self,
        region: str,
        path: str,
        s3client_config: Optional[S3ClientConfig] = None,
        prefix_strategy: Optional[S3PrefixStrategyBase] = None,
        num_copies: int = 1,
        **kwargs,
    ) -> None:
        """
        Initialize an S3 writer for distributed checkpointing.

        Args:
            region (str): The AWS region for S3.
            path (str): The S3 URI to write checkpoints to.
            prefix_strategy: Strategy for generating S3 prefixes.
            kwargs (dict): Keyword arguments to pass to the parent :class:`FileSystemWriter`.
        """
        super().__init__(
            path=path,
            sync_files=False,  # FIXME: setting this to True makes the run to fail (L#333: `os.fsync(stream.fileno())`)
            **kwargs,
        )
        self.fs = S3FileSystem(region, s3client_config=s3client_config)  # type: ignore
        self.path = self.fs.init_path(path)
        self.num_copies = num_copies
        self.prefix_strategy = prefix_strategy or DefaultPrefixStrategy()

    def prepare_global_plan(self, plans: List[SavePlan]) -> List[SavePlan]:
        """
        Prepare save plans with S3-specific storage metadata.

        Args:
            plans: List of save plans to be processed.

        Returns:
            Modified save plans with S3 storage metadata.
        """
        return [
            dataclasses.replace(
                plan, storage_data=StorageMetadata(self.prefix_strategy(idx))
            )
            for idx, plan in enumerate(plans)
        ]
    
    def write_data(
        self,
        plan: SavePlan,
        planner: SavePlanner,
        ):
        if self.num_copies <= 1:
            return super().write_data(plan, planner)
        
        storage_plan = plan.storage_data
        file_count = 0
        
        def gen_file():
            nonlocal file_count
            file_name = f"{storage_plan.prefix}{file_count}{DEFAULT_SUFFIX}"
            file_count += 1
            return file_name
        
        file_queue: queue.Queue = queue.Queue()
        from torch.distributed.checkpoint.filesystem import _split_by_size_and_type
        
        for copy in range(self.num_copies):
            if self.single_file_per_rank:
                for bucket in _split_by_size_and_type(self.thread_count, plan.items):
                    file_name = gen_file()
                    # Store just the copy prefix in the relative path
                    relative_path = f"copy-{copy}/{file_name}"
                    # Full path for the actual file
                    full_path = self.fs.concat_path(self.path, relative_path)
                    # Put the tuple in the queue with the correct relative path
                    file_queue.put((full_path, file_name, bucket))
                file_count = 0
            else:
                for item in plan.items:
                    file_name = gen_file()
                    # Store just the copy prefix in the relative path
                    relative_path = f"copy-{copy}/{file_name}"
                    # Full path for the actual file
                    full_path = self.fs.concat_path(self.path, relative_path)
                    # Put the tuple in the queue with the correct relative path
                    file_queue.put((full_path, relative_path, [item]))
                file_count = 0
    
        return self._write_data(planner, file_queue)
        
    def finish(self, metadata: Metadata, results: list[list[WriteResult]]) -> None:
        """
        Finish the checkpointing process and save the number of copies in metadata

        Args:
            metadata: Metadata for the checkpoint.
            results: List of write results for each rank.
        """
        # Process results normally
        storage_md = {}
        for wr_list in results:
            storage_md.update({wr.index: wr.storage_data for wr in wr_list})
            
        metadata.storage_data = storage_md
        # Add duplication info to metadata
        if metadata.storage_meta is None:
            metadata.storage_meta = StorageMeta()

        # Add num_copies info to modules list
        metadata.storage_meta.modules.append(f"num_copies={self.num_copies}")
            
         # Replace the storage_meta with our extended version
        logging.debug("storage_data: updated METADATA %s", metadata.storage_meta)
        
        tmp_path = cast(Path, self.fs.concat_path(self.path, f"{_metadata_fn}.tmp"))
        with self.fs.create_stream(tmp_path, "wb") as metadata_file:
            pickle.dump(metadata, metadata_file)
            if self.sync_files:
                try:
                    os.fsync(metadata_file.fileno())
                except (AttributeError, io.UnsupportedOperation):
                    os.sync()

        # delete in-case other checkpoints were present.
        if self.fs.exists(self.metadata_path):
            self.fs.rm_file(self.metadata_path)

        self.fs.rename(tmp_path, self.metadata_path)

    @classmethod
    def validate_checkpoint_id(cls, checkpoint_id: Union[str, os.PathLike]) -> bool:
        return S3FileSystem.validate_checkpoint_id(checkpoint_id)


class S3StorageReader(FileSystemReader):
    def __init__(
        self,
        region: str,
        path: Union[str, os.PathLike],
        s3client_config: Optional[S3ClientConfig] = None,
    ) -> None:
        """
        Initialize an S3 reader for distributed checkpointing.

        Args:
            region (str): The AWS region for S3.
            path (Union[str, os.PathLike]): The S3 path to read checkpoints from.
        """
        super().__init__(path)
        self.fs = S3FileSystem(region, s3client_config=s3client_config)  # type: ignore
        self.path = self.fs.init_path(path)
        self.sync_files = False
        self.num_copies = 1
        self.assigned_copy = None
        self.rank = None

    @classmethod
    def validate_checkpoint_id(cls, checkpoint_id: Union[str, os.PathLike]) -> bool:
        return S3FileSystem.validate_checkpoint_id(checkpoint_id)

    def read_metadata(self) -> Metadata:
        """
        Read the metadata from the checkpoint directory including the number of copies present

        Returns:
            Metadata: The metadata for the checkpoint.
        """
        metadata = super().read_metadata()
        logger.debug(f"Storage metadata: {metadata.storage_meta}")
        self.num_copies = int(metadata.storage_meta.modules[0].split("=")[1])
        logger.debug(f"Num of copies: {self.num_copies}")
        # Log all the important info of metadata
        # logger.debug(f"Metadata: {metadata}")
     
        return metadata
    
    def set_up_storage_reader(self, metadata, is_coordinator):
        """
            Assigns each worker a specific copy based on its rank where num_copies equals the total rank, each rank get its own dedictaed copy
        """
        super().set_up_storage_reader(metadata, is_coordinator)
        
        try:
            if dist.is_initialized():
                self.rank = dist.get_rank()
            else:
                self.rank = 0
        except Exception:
            self.rank = 0
            
        if self.num_copies > 1:
            self.assigned_copy = self.rank 
            logger.debug(f"Worker rank {self.rank} assigned to copy {self.assigned_copy}")
        
    def read_data(self, plan: LoadPlan, planner: LoadPlanner) -> Future:
        """Read data from assigned copy

        Args:
            plan (LoadPlan): Load plan for reading
            planner (LoadPlanner): Load planner for reading

        Returns:
            Future: Completes when reading operations is done
        """
        logger.debug(f"Rank {self.rank}: Reading data from assigned copy {self.assigned_copy}")
        logger.debug(f"Number of copies: {self.num_copies}")
        if self.num_copies <= 1 or self.assigned_copy is None:
            logger.debug(f"Using default path for reading: num_copies={self.num_copies}, assigned_copy={self.assigned_copy}")
            return super().read_data(plan, planner)

        original_path = self.path
        logger.debug(f"Rank {self.rank}: Original path for reading: {original_path}")
        
        try:
            copy_path = self.fs.concat_path(self.path, f"copy-{self.assigned_copy}")
            logger.debug(f"Rank {self.rank}: Reading from copy path: {copy_path}")
            self.path = copy_path
            return super().read_data(plan, planner)
        except Exception as e:
            logger.error(f"Rank {self.rank}: Error reading from copy {self.assigned_copy}: {e}")
            raise
        finally:
            logger.debug(f"Rank {self.rank}: Restoring original path: {original_path}")
            self.path = original_path

    

def _path_or_str_to_str(path: Union[str, os.PathLike]) -> str:
    return path if isinstance(path, str) else str(path)

