"""SQLite opened from several threads at once (stores searched side by side)."""

from affiliate_scraper.db import Database


def test_new_database_opened_by_several_threads_at_once(tmp_path):
    """Stores searched side by side each open the database; on a NEW file switching to WAL used to
    fail with 'database is locked' and that store's results were lost."""
    import threading

    fails = []
    for n in range(25):
        path = tmp_path / f"n{n}.db"
        gate = threading.Barrier(4)

        def work():
            try:
                gate.wait()
                db = Database(path)
                db.finish_run(db.start_run("search", 1), 0, [])
                db.close()
            except Exception as exc:  # noqa: BLE001
                fails.append(repr(exc))

        threads = [threading.Thread(target=work) for _ in range(4)]
        [t.start() for t in threads]
        [t.join() for t in threads]
    assert fails == []
