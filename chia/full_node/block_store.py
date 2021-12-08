import logging
from typing import Dict, List, Optional, Tuple, Any

import aiosqlite

from chia.consensus.block_record import BlockRecord
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.types.full_block import FullBlock
from chia.types.weight_proof import SubEpochChallengeSegment, SubEpochSegments
from chia.util.db_wrapper import DBWrapper
from chia.util.ints import uint32, uint8, uint64
from chia.util.lru_cache import LRUCache
from chia.consensus.pot_iterations import is_overflow_block
from chia.consensus.constants import ConsensusConstants
from chia.types.blockchain_format.slots import ChallengeBlockInfo
from chia.types.blockchain_format.sub_epoch_summary import SubEpochSummary
from chia.types.blockchain_format.classgroup import ClassgroupElement
import zstd

log = logging.getLogger(__name__)


class BlockStore:
    db: aiosqlite.Connection
    block_cache: LRUCache
    db_wrapper: DBWrapper
    ses_challenge_cache: LRUCache
    constants: ConsensusConstants

    @classmethod
    async def create(cls, db_wrapper: DBWrapper, constants: ConsensusConstants):
        self = cls()

        # All full blocks which have been added to the blockchain. Header_hash -> block
        self.db_wrapper = db_wrapper
        self.db = db_wrapper.db
        self.constants = constants

        if self.db_wrapper.db_version == 2:

            # TODO: move some of these fields into the block blob. One reason we keep
            # a copy prev_hash in thes table is because it's slow to parse the
            # FullBlock. Once we've made that fast, we can move many of these
            # fields into the blob, to save space
            await self.db.execute(
                "CREATE TABLE IF NOT EXISTS full_blocks("
                "header_hash blob PRIMARY KEY,"
                "prev_hash blob,"
                "height bigint,"
                "sub_slot_iters blob,"
                "required_iters blob,"
                "deficit tinyint,"
                "prev_transaction_height int,"
                "sub_epoch_summary blob,"
                "is_fully_compactified tinyint,"
                "block blob)"
            )

            # This is a single-row table containing the hash of the current
            # peak. The "key" field is there to make update statements simple
            await self.db.execute("CREATE TABLE IF NOT EXISTS current_peak(key int PRIMARY KEY, hash blob)")

            # Sub epoch segments for weight proofs
            await self.db.execute(
                "CREATE TABLE IF NOT EXISTS sub_epoch_segments_v3("
                "ses_block_hash blob PRIMARY KEY,"
                "challenge_segments blob)"
            )

            # Height index so we can look up in order of height for sync purposes
            await self.db.execute("CREATE INDEX IF NOT EXISTS height on full_blocks(height)")
            await self.db.execute(
                "CREATE INDEX IF NOT EXISTS is_fully_compactified on full_blocks(is_fully_compactified)"
            )

        else:

            await self.db.execute(
                "CREATE TABLE IF NOT EXISTS full_blocks(header_hash text PRIMARY KEY, height bigint,"
                "  is_block tinyint, is_fully_compactified tinyint, block blob)"
            )

            # Block records
            await self.db.execute(
                "CREATE TABLE IF NOT EXISTS block_records(header_hash "
                "text PRIMARY KEY, prev_hash text, height bigint,"
                "block blob, sub_epoch_summary blob, is_peak tinyint, is_block tinyint)"
            )

            # Sub epoch segments for weight proofs
            await self.db.execute(
                "CREATE TABLE IF NOT EXISTS sub_epoch_segments_v3(ses_block_hash text PRIMARY KEY,"
                "challenge_segments blob)"
            )

            # Height index so we can look up in order of height for sync purposes
            await self.db.execute("CREATE INDEX IF NOT EXISTS full_block_height on full_blocks(height)")
            await self.db.execute(
                "CREATE INDEX IF NOT EXISTS is_fully_compactified on full_blocks(is_fully_compactified)"
            )

            await self.db.execute("CREATE INDEX IF NOT EXISTS height on block_records(height)")

            if self.db_wrapper.allow_upgrades:
                await self.db.execute("DROP INDEX IF EXISTS hh")
                await self.db.execute("DROP INDEX IF EXISTS is_block")
                await self.db.execute("DROP INDEX IF EXISTS peak")
                await self.db.execute(
                    "CREATE INDEX IF NOT EXISTS is_peak_eq_1_idx on block_records(is_peak) where is_peak = 1"
                )
            else:
                await self.db.execute("CREATE INDEX IF NOT EXISTS peak on block_records(is_peak) where is_peak = 1")

        await self.db.commit()
        self.block_cache = LRUCache(1000)
        self.ses_challenge_cache = LRUCache(50)
        return self

    def maybe_from_hex(self, field: Any) -> bytes:
        if self.db_wrapper.db_version == 2:
            return field
        else:
            return bytes.fromhex(field)

    def maybe_to_hex(self, field: bytes) -> Any:
        if self.db_wrapper.db_version == 2:
            return field
        else:
            return field.hex()

    def compress(self, block: FullBlock) -> bytes:
        return zstd.compress(bytes(block))

    def maybe_decompress(self, block_bytes: bytes) -> FullBlock:
        if self.db_wrapper.db_version == 2:
            return FullBlock.from_bytes(zstd.decompress(block_bytes))
        else:
            return FullBlock.from_bytes(block_bytes)

    async def add_full_block(self, header_hash: bytes32, block: FullBlock, block_record: BlockRecord) -> None:
        self.block_cache.put(header_hash, block)

        if self.db_wrapper.db_version == 2:

            ses: Optional[bytes] = (
                None
                if block_record.sub_epoch_summary_included is None
                else bytes(block_record.sub_epoch_summary_included)
            )

            cursor_1 = await self.db.execute(
                "INSERT OR REPLACE INTO full_blocks VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    header_hash,
                    block.prev_header_hash,
                    block.height,
                    bytes(block_record.sub_slot_iters),
                    bytes(block_record.required_iters),
                    block_record.deficit,
                    block_record.prev_transaction_block_height,
                    ses,
                    int(block.is_fully_compactified()),
                    self.compress(block),
                ),
            )
            await cursor_1.close()

        else:
            cursor_1 = await self.db.execute(
                "INSERT OR REPLACE INTO full_blocks VALUES(?, ?, ?, ?, ?)",
                (
                    header_hash.hex(),
                    block.height,
                    int(block.is_transaction_block()),
                    int(block.is_fully_compactified()),
                    bytes(block),
                ),
            )
            await cursor_1.close()

            cursor_2 = await self.db.execute(
                "INSERT OR REPLACE INTO block_records VALUES(?, ?, ?, ?,?, ?, ?)",
                (
                    header_hash.hex(),
                    block.prev_header_hash.hex(),
                    block.height,
                    bytes(block_record),
                    None
                    if block_record.sub_epoch_summary_included is None
                    else bytes(block_record.sub_epoch_summary_included),
                    False,
                    block.is_transaction_block(),
                ),
            )
            await cursor_2.close()

    async def persist_sub_epoch_challenge_segments(
        self, ses_block_hash: bytes32, segments: List[SubEpochChallengeSegment]
    ) -> None:
        async with self.db_wrapper.lock:
            cursor_1 = await self.db.execute(
                "INSERT OR REPLACE INTO sub_epoch_segments_v3 VALUES(?, ?)",
                (self.maybe_to_hex(ses_block_hash), bytes(SubEpochSegments(segments))),
            )
            await cursor_1.close()
            await self.db.commit()

    async def get_sub_epoch_challenge_segments(
        self,
        ses_block_hash: bytes32,
    ) -> Optional[List[SubEpochChallengeSegment]]:
        cached = self.ses_challenge_cache.get(ses_block_hash)
        if cached is not None:
            return cached

        cursor = await self.db.execute(
            "SELECT challenge_segments from sub_epoch_segments_v3 WHERE ses_block_hash=?",
            (self.maybe_to_hex(ses_block_hash),),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is not None:
            challenge_segments = SubEpochSegments.from_bytes(row[0]).challenge_segments
            self.ses_challenge_cache.put(ses_block_hash, challenge_segments)
            return challenge_segments
        return None

    def rollback_cache_block(self, header_hash: bytes32):
        try:
            self.block_cache.remove(header_hash)
        except KeyError:
            # this is best effort. When rolling back, we may not have added the
            # block to the cache yet
            pass

    async def get_full_block(self, header_hash: bytes32) -> Optional[FullBlock]:
        cached = self.block_cache.get(header_hash)
        if cached is not None:
            log.debug(f"cache hit for block {header_hash.hex()}")
            return cached
        log.debug(f"cache miss for block {header_hash.hex()}")
        cursor = await self.db.execute(
            "SELECT block from full_blocks WHERE header_hash=?", (self.maybe_to_hex(header_hash),)
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is not None:
            block = self.maybe_decompress(row[0])
            self.block_cache.put(header_hash, block)
            return block
        return None

    async def get_full_block_bytes(self, header_hash: bytes32) -> Optional[bytes]:
        cached = self.block_cache.get(header_hash)
        if cached is not None:
            log.debug(f"cache hit for block {header_hash.hex()}")
            return bytes(cached)
        log.debug(f"cache miss for block {header_hash.hex()}")
        cursor = await self.db.execute(
            "SELECT block from full_blocks WHERE header_hash=?", (self.maybe_to_hex(header_hash),)
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is not None:
            if self.db_wrapper.db_version == 2:
                return zstd.decompress(row[0])
            else:
                return row[0]

        return None

    async def get_full_blocks_at(self, heights: List[uint32]) -> List[FullBlock]:
        if len(heights) == 0:
            return []

        heights_db = tuple(heights)
        formatted_str = f'SELECT block from full_blocks WHERE height in ({"?," * (len(heights_db) - 1)}?)'
        cursor = await self.db.execute(formatted_str, heights_db)
        rows = await cursor.fetchall()
        await cursor.close()
        return [self.maybe_decompress(row[0]) for row in rows]

    async def get_block_records_by_hash(self, header_hashes: List[bytes32]):
        """
        Returns a list of Block Records, ordered by the same order in which header_hashes are passed in.
        Throws an exception if the blocks are not present
        """
        if len(header_hashes) == 0:
            return []

        all_blocks: Dict[bytes32, BlockRecord] = {}
        if self.db_wrapper.db_version == 2:
            cursor = await self.db.execute(
                "SELECT block,"
                "sub_slot_iters,"
                "required_iters,"
                "deficit,"
                "prev_transaction_height,"
                "sub_epoch_summary,"
                f'header_hash FROM full_blocks WHERE header_hash in ({"?," * (len(header_hashes) - 1)}?)',
                tuple(header_hashes),
            )
            rows = await cursor.fetchall()
            await cursor.close()
            for row in rows:
                header_hash = bytes32(row[6])
                all_blocks[header_hash] = self.make_block_record(row)
        else:
            formatted_str = f'SELECT block from block_records WHERE header_hash in ({"?," * (len(header_hashes) - 1)}?)'
            cursor = await self.db.execute(formatted_str, tuple([hh.hex() for hh in header_hashes]))
            rows = await cursor.fetchall()
            await cursor.close()
            for row in rows:
                block_rec: BlockRecord = BlockRecord.from_bytes(row[0])
                all_blocks[block_rec.header_hash] = block_rec

        ret: List[BlockRecord] = []
        for hh in header_hashes:
            if hh not in all_blocks:
                raise ValueError(f"Header hash {hh} not in the blockchain")
            ret.append(all_blocks[hh])
        return ret

    async def get_blocks_by_hash(self, header_hashes: List[bytes32]) -> List[FullBlock]:
        """
        Returns a list of Full Blocks blocks, ordered by the same order in which header_hashes are passed in.
        Throws an exception if the blocks are not present
        """

        if len(header_hashes) == 0:
            return []

        header_hashes_db: Tuple[Any, ...]
        if self.db_wrapper.db_version == 2:
            header_hashes_db = tuple(header_hashes)
        else:
            header_hashes_db = tuple([hh.hex() for hh in header_hashes])
        formatted_str = (
            f'SELECT header_hash, block from full_blocks WHERE header_hash in ({"?," * (len(header_hashes_db) - 1)}?)'
        )
        cursor = await self.db.execute(formatted_str, header_hashes_db)
        rows = await cursor.fetchall()
        await cursor.close()
        all_blocks: Dict[bytes32, FullBlock] = {}
        for row in rows:
            header_hash = self.maybe_from_hex(row[0])
            full_block: FullBlock = self.maybe_decompress(row[1])
            # TODO: address hint error and remove ignore
            #       error: Invalid index type "bytes" for "Dict[bytes32, FullBlock]"; expected type "bytes32"  [index]
            all_blocks[header_hash] = full_block  # type: ignore[index]
            self.block_cache.put(header_hash, full_block)
        ret: List[FullBlock] = []
        for hh in header_hashes:
            if hh not in all_blocks:
                raise ValueError(f"Header hash {hh} not in the blockchain")
            ret.append(all_blocks[hh])
        return ret

    async def get_block_record(self, header_hash: bytes32) -> Optional[BlockRecord]:

        if self.db_wrapper.db_version == 2:

            cursor = await self.db.execute(
                "SELECT block,"
                "sub_slot_iters,"
                "required_iters,"
                "deficit,"
                "prev_transaction_height,"
                "sub_epoch_summary,"
                "header_hash FROM full_blocks WHERE header_hash=?",
                (header_hash,),
            )
            row = await cursor.fetchone()
            await cursor.close()
            if row is not None:
                return self.make_block_record(row)

        else:
            cursor = await self.db.execute(
                "SELECT block from block_records WHERE header_hash=?",
                (header_hash.hex(),),
            )
            row = await cursor.fetchone()
            await cursor.close()
            if row is not None:
                return BlockRecord.from_bytes(row[0])
        return None

    async def get_block_records_in_range(
        self,
        start: int,
        stop: int,
    ) -> Dict[bytes32, BlockRecord]:
        """
        Returns a dictionary with all blocks in range between start and stop
        if present.
        """

        ret: Dict[bytes32, BlockRecord] = {}
        if self.db_wrapper.db_version == 2:

            cursor = await self.db.execute(
                "SELECT block,"
                "sub_slot_iters,"
                "required_iters,"
                "deficit,"
                "prev_transaction_height,"
                "sub_epoch_summary,"
                "header_hash FROM full_blocks WHERE height >= ? AND height <= ?",
                (start, stop),
            )
            rows = await cursor.fetchall()
            await cursor.close()
            for row in rows:
                header_hash = bytes32(row[6])
                ret[header_hash] = self.make_block_record(row)

        else:

            formatted_str = f"SELECT header_hash, block from block_records WHERE height >= {start} and height <= {stop}"

            cursor = await self.db.execute(formatted_str)
            rows = await cursor.fetchall()
            await cursor.close()
            for row in rows:
                # TODO: address hint error and remove ignore
                #       error: Invalid index type "bytes" for "Dict[bytes32, BlockRecord]";
                # expected type "bytes32"  [index]
                header_hash = bytes32(self.maybe_from_hex(row[0]))
                ret[header_hash] = BlockRecord.from_bytes(row[1])  # type: ignore[index]

        return ret

    async def get_peak(self) -> Optional[Tuple[bytes32, uint32]]:

        if self.db_wrapper.db_version == 2:
            cursor = await self.db.execute("SELECT hash FROM current_peak WHERE key = 0")
            peak_row = await cursor.fetchone()
            await cursor.close()
            if peak_row is None:
                return None
            cursor_2 = await self.db.execute("SELECT height FROM full_blocks WHERE header_hash=?", (peak_row[0],))
            peak_height = await cursor_2.fetchone()
            await cursor_2.close()
            if peak_height is None:
                return None
            return bytes32(peak_row[0]), uint32(peak_height[0])
        else:
            res = await self.db.execute("SELECT header_hash, height from block_records WHERE is_peak = 1")
            peak_row = await res.fetchone()
            await res.close()
            if peak_row is None:
                return None
            return bytes32(bytes.fromhex(peak_row[0])), uint32(peak_row[1])

    async def get_block_records_close_to_peak(
        self, blocks_n: int
    ) -> Tuple[Dict[bytes32, BlockRecord], Optional[bytes32]]:
        """
        Returns a dictionary with all blocks that have height >= peak height - blocks_n, as well as the
        peak header hash.
        """

        peak = await self.get_peak()
        if peak is None:
            return {}, None

        ret: Dict[bytes32, BlockRecord] = {}
        if self.db_wrapper.db_version == 2:

            cursor = await self.db.execute(
                "SELECT block,"
                "sub_slot_iters,"
                "required_iters,"
                "deficit,"
                "prev_transaction_height,"
                "sub_epoch_summary,"
                "header_hash FROM full_blocks WHERE height >= ?",
                (peak[1] - blocks_n,),
            )
            rows = await cursor.fetchall()
            await cursor.close()
            for row in rows:
                header_hash = bytes32(row[6])
                ret[header_hash] = self.make_block_record(row)

        else:
            formatted_str = f"SELECT header_hash, block  from block_records WHERE height >= {peak[1] - blocks_n}"
            cursor = await self.db.execute(formatted_str)
            rows = await cursor.fetchall()
            await cursor.close()
            for row in rows:
                header_hash = bytes32(self.maybe_from_hex(row[0]))
                ret[header_hash] = BlockRecord.from_bytes(row[1])

        return ret, peak[0]

    async def set_peak(self, header_hash: bytes32) -> None:
        # We need to be in a sqlite transaction here.
        # Note: we do not commit this to the database yet, as we need to also change the coin store

        if self.db_wrapper.db_version == 2:
            # Note: we use the key field as 0 just to ensure all inserts replace the existing row
            cursor = await self.db.execute("INSERT OR REPLACE INTO current_peak VALUES(?, ?)", (0, header_hash))
            await cursor.close()
        else:
            cursor_1 = await self.db.execute("UPDATE block_records SET is_peak=0 WHERE is_peak=1")
            await cursor_1.close()
            cursor_2 = await self.db.execute(
                "UPDATE block_records SET is_peak=1 WHERE header_hash=?",
                (self.maybe_to_hex(header_hash),),
            )
            await cursor_2.close()

    async def is_fully_compactified(self, header_hash: bytes32) -> Optional[bool]:
        cursor = await self.db.execute(
            "SELECT is_fully_compactified from full_blocks WHERE header_hash=?", (self.maybe_to_hex(header_hash),)
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        return bool(row[0])

    async def get_random_not_compactified(self, number: int) -> List[int]:
        # Since orphan blocks do not get compactified, we need to check whether all blocks with a
        # certain height are not compact. And if we do have compact orphan blocks, then all that
        # happens is that the occasional chain block stays uncompact - not ideal, but harmless.
        cursor = await self.db.execute(
            f"SELECT height FROM full_blocks GROUP BY height HAVING sum(is_fully_compactified)=0 "
            f"ORDER BY RANDOM() LIMIT {number}"
        )
        rows = await cursor.fetchall()
        await cursor.close()

        heights = []
        for row in rows:
            heights.append(int(row[0]))

        return heights

    def make_block_record(self, row: Tuple[bytes, bytes, bytes, uint8, uint32, bytes, bytes32]) -> BlockRecord:

        header_hash = row[6]
        block = FullBlock.from_bytes(zstd.decompress(row[0]))

        sub_slot_iters = uint64.from_bytes(row[1])
        required_iters = uint64.from_bytes(row[2])
        ses: SubEpochSummary = None if row[5] is None else SubEpochSummary.from_bytes(row[5])
        deficit: uint8 = row[3]
        prev_transaction_block_height: uint32 = row[4]

        if block.reward_chain_block.infused_challenge_chain_ip_vdf is not None:
            icc_output: Optional[ClassgroupElement] = block.reward_chain_block.infused_challenge_chain_ip_vdf.output
        else:
            icc_output = None

        cbi = ChallengeBlockInfo(
            block.reward_chain_block.proof_of_space,
            block.reward_chain_block.challenge_chain_sp_vdf,
            block.reward_chain_block.challenge_chain_sp_signature,
            block.reward_chain_block.challenge_chain_ip_vdf,
        )

        overflow = is_overflow_block(self.constants, block.reward_chain_block.signage_point_index)

        if len(block.finished_sub_slots) > 0:
            finished_challenge_slot_hashes: Optional[List[bytes32]] = [
                sub_slot.challenge_chain.get_hash() for sub_slot in block.finished_sub_slots
            ]
            finished_reward_slot_hashes: Optional[List[bytes32]] = [
                sub_slot.reward_chain.get_hash() for sub_slot in block.finished_sub_slots
            ]
            finished_infused_challenge_slot_hashes: Optional[List[bytes32]] = [
                sub_slot.infused_challenge_chain.get_hash()
                for sub_slot in block.finished_sub_slots
                if sub_slot.infused_challenge_chain is not None
            ]
        elif block.height == 0:
            finished_challenge_slot_hashes = [self.constants.GENESIS_CHALLENGE]
            finished_reward_slot_hashes = [self.constants.GENESIS_CHALLENGE]
            finished_infused_challenge_slot_hashes = None
        else:
            finished_challenge_slot_hashes = None
            finished_reward_slot_hashes = None
            finished_infused_challenge_slot_hashes = None
        prev_transaction_block_hash = (
            block.foliage_transaction_block.prev_transaction_block_hash
            if block.foliage_transaction_block is not None
            else None
        )
        timestamp = block.foliage_transaction_block.timestamp if block.foliage_transaction_block is not None else None
        fees = block.transactions_info.fees if block.transactions_info is not None else None
        reward_claims_incorporated = (
            block.transactions_info.reward_claims_incorporated if block.transactions_info is not None else None
        )

        return BlockRecord(
            header_hash,
            block.prev_header_hash,
            block.height,
            block.weight,
            block.total_iters,
            block.reward_chain_block.signage_point_index,
            block.reward_chain_block.challenge_chain_ip_vdf.output,
            icc_output,
            block.reward_chain_block.get_hash(),
            cbi.get_hash(),
            sub_slot_iters,
            block.foliage.foliage_block_data.pool_target.puzzle_hash,
            block.foliage.foliage_block_data.farmer_reward_puzzle_hash,
            required_iters,
            deficit,
            overflow,
            prev_transaction_block_height,
            timestamp,
            prev_transaction_block_hash,
            fees,
            reward_claims_incorporated,
            finished_challenge_slot_hashes,
            finished_infused_challenge_slot_hashes,
            finished_reward_slot_hashes,
            ses,
        )
