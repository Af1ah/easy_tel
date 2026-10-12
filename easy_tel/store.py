import sqlite3


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("""CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY, chat INTEGER NOT NULL, message INTEGER NOT NULL,
            name TEXT NOT NULL, size INTEGER NOT NULL, status TEXT NOT NULL,
            UNIQUE(chat, message))""")
        self.db.execute("UPDATE jobs SET status='queued' WHERE status='running'")
        self.db.commit()

    def add(self, chat, message, name, size):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO jobs(chat,message,name,size,status) VALUES(?,?,?,?,'queued')",
                            (chat, message, name, size))
        return self.db.execute("SELECT * FROM jobs WHERE chat=? AND message=?", (chat, message)).fetchone()

    def pending_count(self):
        return self.db.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0]

    def next(self):
        return self.db.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY id LIMIT 1").fetchone()

    def status(self, job_id, status):
        with self.db:
            self.db.execute("UPDATE jobs SET status=? WHERE id=?", (status, job_id))

    def recent(self, chat):
        return self.db.execute("SELECT id,status FROM jobs WHERE chat=? ORDER BY id DESC LIMIT 10", (chat,)).fetchall()
