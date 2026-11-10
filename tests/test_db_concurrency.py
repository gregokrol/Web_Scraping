"""The database is opened by several threads at once (a search runs one scrape per store side by side)."""

import threading

from affiliate_scraper.db import Database


def test_new_database_opened_by_many_threads_at_once(tmp_path):
    # SQLite does not wait for a busy lock while switching a new file to WAL - without the retry about
    # 1 in 15 pairs of connections failed with "database is locked" (one store of a search was lost).
    errors = []
    for i in range(25):
        path = tmp_path / f"d{i}.db"
        start = threading.Barrier(6)

        def open_it():
            start.wait()
            try:
                Database(path).close()
            except Exception as exc:  # collected, asserted below
                errors.append(repr(exc))

        threads = [threading.Thread(target=open_it) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert errors == []
    db = Database(tmp_path / "d0.db")
    assert db.query("PRAGMA journal_mode")[0][0] == "wal"
    assert len(db.query("SELECT * FROM schema_info")) == 1
    db.close()
