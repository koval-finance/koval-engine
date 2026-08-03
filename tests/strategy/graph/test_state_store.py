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
