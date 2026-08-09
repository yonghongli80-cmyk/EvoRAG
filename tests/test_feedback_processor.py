"""反馈驱动检索优化测试。"""
import os
import tempfile


def test_feedback_processor_creates_table():
    from rag.feedback_processor import FeedbackProcessor
    db_path = tempfile.mktemp(suffix=".db")
    try:
        fp = FeedbackProcessor(db_path)
        import sqlite3
        conn = sqlite3.connect(db_path)
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='chunk_feedback'").fetchall()
        assert len(rows) == 1
        conn.close()
        fp.close()
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)


def test_feedback_processor_updates_weight():
    from rag.feedback_processor import FeedbackProcessor
    db_path = tempfile.mktemp(suffix=".db")
    try:
        fp = FeedbackProcessor(db_path)
        fp.record_feedback("abc123", "negative")
        weight = fp.get_weight("abc123")
        assert weight < 1.0, f"negative 反馈后 weight 应 < 1.0，实际 {weight}"
        fp.close()
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)


def test_feedback_processor_positive_increases_weight():
    from rag.feedback_processor import FeedbackProcessor
    db_path = tempfile.mktemp(suffix=".db")
    try:
        fp = FeedbackProcessor(db_path)
        fp.record_feedback("abc123", "positive")
        weight = fp.get_weight("abc123")
        assert weight > 1.0
        fp.close()
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)


def test_feedback_processor_weight_bounds():
    from rag.feedback_processor import FeedbackProcessor
    db_path = tempfile.mktemp(suffix=".db")
    try:
        fp = FeedbackProcessor(db_path)
        for _ in range(20):
            fp.record_feedback("abc123", "negative")
        assert fp.get_weight("abc123") >= 0.2
        for _ in range(40):
            fp.record_feedback("def456", "positive")
        assert fp.get_weight("def456") <= 2.0
        fp.close()
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)


def test_feedback_processor_dedup_user():
    from rag.feedback_processor import FeedbackProcessor
    db_path = tempfile.mktemp(suffix=".db")
    try:
        fp = FeedbackProcessor(db_path)
        fp.record_feedback("abc123", "negative", user_id=1)
        fp.record_feedback("abc123", "negative", user_id=1)
        weight = fp.get_weight("abc123")
        assert weight == 0.9
        fp.close()
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)


def test_feedback_processor_counts_distinct_user_feedback():
    from rag.feedback_processor import FeedbackProcessor
    db_path = tempfile.mktemp(suffix=".db")
    try:
        fp = FeedbackProcessor(db_path)
        fp.record_feedback("abc123", "negative", user_id=1)
        fp.record_feedback("abc123", "negative", user_id=2)
        assert fp.get_weight("abc123") == 0.8
        fp.close()
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)


def test_feedback_processor_decay():
    from rag.feedback_processor import FeedbackProcessor
    db_path = tempfile.mktemp(suffix=".db")
    try:
        fp = FeedbackProcessor(db_path)
        fp.record_feedback("abc123", "negative")
        fp.record_feedback("abc123", "negative")
        before = fp.get_weight("abc123")
        fp.decay_weights()
        after = fp.get_weight("abc123")
        assert after > before
        fp.close()
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)


def test_get_weights_for_chunks():
    from rag.feedback_processor import FeedbackProcessor
    db_path = tempfile.mktemp(suffix=".db")
    try:
        fp = FeedbackProcessor(db_path)
        fp.record_feedback("abc123", "negative")
        fp.record_feedback("def456", "positive")
        weights = fp.get_weights(["abc123", "def456", "unknown"])
        assert "abc123" in weights
        assert "def456" in weights
        assert weights["unknown"] == 1.0
        fp.close()
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)
