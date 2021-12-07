from clvm_tools import binutils
from chia.types.blockchain_format.program import Program, INFINITE_COST
from chia.types.announcement import Announcement
from chia.util.ints import uint64
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.wallet.puzzles.load_clvm import load_clvm
from chia.types.blockchain_format.coin import Coin

SINGLETON_MOD = load_clvm("singleton_top_layer.clvm")
LAUNCHER_PUZZLE = load_clvm("singleton_launcher.clvm")
DID_MOD = load_clvm("did_innerpuz.clvm")
NFT_MOD = load_clvm("nft_innerpuz.clvm")
LAUNCHER_PUZZLE_HASH = LAUNCHER_PUZZLE.get_tree_hash()
SINGLETON_MOD_HASH = SINGLETON_MOD.get_tree_hash()
NFT_MOD_HASH = NFT_MOD.get_tree_hash()
LAUNCHER_ID = Program.to(b"launcher-id").get_tree_hash()


def test_transfer_no_backpayments():
    did_one: bytes32 = Program.to("did_one").get_tree_hash()
    did_two: bytes32 = Program.to("did_two").get_tree_hash()

    did_one_pk: bytes32 = Program.to("did_one_pk").get_tree_hash()
    did_one_innerpuz = DID_MOD.curry(did_one_pk, 0, 0)
    SINGLETON_STRUCT = Program.to((SINGLETON_MOD_HASH, (did_one, LAUNCHER_PUZZLE_HASH)))
    did_one_puzzle: bytes32 = SINGLETON_MOD.curry(SINGLETON_STRUCT, did_one_innerpuz)
    did_one_parent: bytes32 = Program.to("did_one_parent").get_tree_hash()
    did_one_amount = 201

    did_two_pk: bytes32 = Program.to("did_two_pk").get_tree_hash()
    did_two_innerpuz = DID_MOD.curry(did_one_pk, 0, 0)
    SINGLETON_STRUCT = Program.to((SINGLETON_MOD_HASH, (did_two, LAUNCHER_PUZZLE_HASH)))
    did_two_puzzle: bytes32 = SINGLETON_MOD.curry(SINGLETON_STRUCT, did_two_innerpuz)
    did_two_parent: bytes32 = Program.to("did_two_parent").get_tree_hash()
    did_two_amount = 401

    did_one_coin = Coin(did_one_parent, did_one_puzzle.get_tree_hash(), did_one_amount)
    did_two_coin = Coin(did_two_parent, did_two_puzzle.get_tree_hash(), did_two_amount)
    # NFT_MOD_HASH
    # SINGLETON_STRUCT
    # CURRENT_OWNER_DID
    # OPTIONAL_CREATOR_FEE_PUZHASH
    # OPTIONAL_CREATOR_FEE_CALCULATOR
    # my_puzhash
    # my_amount
    # my_did_inner
    # my_did_amount
    # my_did_parent
    # new_did
    # new_did_parent
    # new_did_inner
    # new_did_amount
    # trade_price
    trade_price = 0
    solution = Program.to(
        [
            NFT_MOD_HASH,  # curried in params
            SINGLETON_STRUCT,
            did_one,
            0,
            0,  # below here is the solution
            Program.to("my_puzzlehash").get_tree_hash(),
            uint64(1),
            did_one_innerpuz.get_tree_hash(),
            did_one_amount,
            did_one_parent,
            did_two,
            did_two_parent,
            did_two_innerpuz.get_tree_hash(),
            did_two_amount,
            trade_price,
        ]
    )
    cost, res = NFT_MOD.run_with_cost(INFINITE_COST, solution)
    ann = bytes(bytes(trade_price) + did_two)
    announcement_one = Announcement(did_one_coin.name(), ann)
    announcement_two = Announcement(did_two_coin.name(), bytes(trade_price))
    assert res.rest().first().first().as_int() == 61
    assert res.rest().first().rest().first().as_atom() == announcement_two.name()
    assert res.rest().rest().first().first().as_int() == 61
    assert res.rest().rest().first().rest().first().as_atom() == announcement_one.name()