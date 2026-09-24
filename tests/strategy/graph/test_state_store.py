import pickle

from koval.strategy.graph.state_store import NodeStateStore


def test_state_is_per_node_and_persists():
    store = NodeStateStore()
    a = store.get("node_a")
    a["count"] = 1
    a["count"] += 1
    # same node id returns the same mutable dict
    assert store.get("node_a")["count"] == 2
    # different node id is isolated
    assert store.get("node_b") == {}


def test_store_pickle_roundtrip():
    store = NodeStateStore()
    store.get("n")["x"] = 42
    restored = pickle.loads(pickle.dumps(store))
    assert restored.get("n")["x"] == 42


def test_checkpoint_is_an_independent_copy_and_can_be_restored():
    store = NodeStateStore()
    store.get("n")["values"] = [1, 2]

    checkpoint = store.checkpoint()
    checkpoint["n"]["values"].append(3)
    assert store.get("n")["values"] == [1, 2]

    store.restore({"n": {"values": [4]}})
    assert store.checkpoint() == {"n": {"values": [4]}}
