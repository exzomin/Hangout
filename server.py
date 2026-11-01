#!/usr/bin/env python3
"""Hangout server: accounts, friends, direct messages and hangout rooms (up to 5 people).

Run it on a machine your friends can reach:   python server.py        (port 5000)
Another port:                                  python server.py 6000
Everything is saved in hangout.db next to this file. No extra packages needed."""
import hashlib, hmac, json, os, queue, re, secrets, socket, sqlite3, sys, threading, time

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("PORT", 5000))
DB_FILE = os.environ.get("HANGOUT_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "hangout.db"))
MAX_ROOM = 5
MAX_LINE = 1_000_000  # biggest single message accepted (a screen frame is ~50 KB)
MAX_FRIENDS, MAX_PENDING, MAX_THREADS = 200, 50, 1000  # each person uses 2 threads
NAME_RE = re.compile(r"^[A-Za-z0-9_]{3,20}$")
LINES = [(0, 1, 2), (3, 4, 5), (6, 7, 8), (0, 3, 6), (1, 4, 7), (2, 5, 8), (0, 4, 8), (2, 4, 6)]
MEDIA = ("v", "scr", "scr_stop")  # voice and screen data: sent without taking the big lock

LOCK = threading.RLock()  # protects the database and the online/rooms tables
db = sqlite3.connect(DB_FILE, check_same_thread=False)
db.executescript("""
CREATE TABLE IF NOT EXISTS users(name TEXT PRIMARY KEY COLLATE NOCASE, salt BLOB, pw BLOB);
CREATE TABLE IF NOT EXISTS friends(a TEXT COLLATE NOCASE, b TEXT COLLATE NOCASE, PRIMARY KEY(a, b));
CREATE TABLE IF NOT EXISTS requests(src TEXT COLLATE NOCASE, dst TEXT COLLATE NOCASE, PRIMARY KEY(src, dst));
CREATE TABLE IF NOT EXISTS dms(id INTEGER PRIMARY KEY AUTOINCREMENT, src TEXT COLLATE NOCASE,
                               dst TEXT COLLATE NOCASE, text TEXT, ts INTEGER, seen INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS dms_pair ON dms(src, dst);
""")

online = {}  # username -> Conn
rooms = {}  # room id -> Room


def sql(query, *args):
    with LOCK:
        rows = db.execute(query, args).fetchall()
        db.commit()
        return rows


def changed(query, *args):  # like sql() but returns how many rows were touched
    with LOCK:
        n = db.execute(query, args).rowcount
        db.commit()
        return n


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# ---------- one connected person ----------
class Conn:
    def __init__(self, sock):
        self.sock, self.user, self.room, self.alive = sock, None, None, True
        self.out = queue.Queue()  # a writer thread empties this, so a slow person never blocks anyone else

    def send(self, m, media=False):
        if not self.alive:
            return
        if media and self.out.qsize() > 40:  # slow listener: drop voice/video instead of lagging
            return
        if self.out.qsize() > 2000:
            return self.close()
        self.out.put((json.dumps(m, separators=(",", ":")) + "\n").encode())

    def writer(self):
        while True:
            data = self.out.get()
            if data is None:
                break
            try:
                self.sock.sendall(data)
            except OSError:
                break
        self.close()

    def close(self, flush=False):
        if flush:  # let the writer send what is queued first, then it closes the socket
            self.out.put(None)
            return
        self.alive = False
        self.out.put(None)
        for f in (lambda: self.sock.shutdown(socket.SHUT_RDWR), self.sock.close):
            try:
                f()
            except OSError:
                pass


# ---------- one hangout room ----------
class Room:
    def __init__(self, rid):
        self.id, self.members, self.invited, self.picks = rid, [], set(), {}
        self.reset()

    def reset(self):
        self.board, self.turn, self.res = [""] * 9, "X", ""

    def names(self):
        return [c.user for c in self.members]

    def send(self, m, skip=None, media=False):
        for c in list(self.members):
            if c is not skip:
                c.send(m, media)

    def ttt(self):  # the first two people in the room play, everyone else watches
        p = self.names()
        return {"t": "ttt_state", "b": list(self.board), "turn": self.turn, "w": self.res,
                "p": {"X": p[0] if p else "", "O": p[1] if len(p) > 1 else ""}}

    def rps(self):
        p = self.names()
        done = len(p) > 1 and all(n in self.picks for n in p)
        return {"t": "rps_state", "n": len(p), "p": [n for n in p if n in self.picks],
                "r": dict(self.picks) if done else None}

    def push_all(self):
        self.send({"t": "names", "d": self.names()})
        self.send(self.ttt())
        self.send(self.rps())


def leave_room(c):
    with LOCK:
        r, c.room = c.room, None
        if not r or c not in r.members:
            return
        if r.members.index(c) < 2:  # a player left: start the board over
            r.reset()
        r.members.remove(c)
        r.picks.pop(c.user, None)
        if r.members:
            r.push_all()
        else:
            rooms.pop(r.id, None)


def join_room(c, r):
    leave_room(c)
    r.members.append(c)
    r.invited.discard(c.user)
    c.room = r
    c.send({"t": "room", "id": r.id})
    r.push_all()


# ---------- accounts ----------
def hash_pw(pw, salt):
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 150_000)


def text(m, key, limit):
    v = m[key]
    if not isinstance(v, str):
        raise ValueError(key)
    return v[:limit]


def canon(name):  # the username exactly as it was registered (names are not case sensitive)
    rows = sql("SELECT name FROM users WHERE name=?", name.strip())
    return rows[0][0] if rows else None


def are_friends(a, b):
    return bool(sql("SELECT 1 FROM friends WHERE a=? AND b=?", a, b))


def auth(c, m):
    name, pw = text(m, "n", 40).strip(), text(m, "p", 200)
    if m["t"] == "register":
        if not NAME_RE.match(name):
            return c.send({"t": "autherr", "x": "Usernames are 3-20 letters, numbers or _"})
        if len(pw) < 6:
            return c.send({"t": "autherr", "x": "Use a password with at least 6 characters"})
        salt = secrets.token_bytes(16)
        try:
            changed("INSERT INTO users VALUES(?,?,?)", name, salt, hash_pw(pw, salt))
        except sqlite3.IntegrityError:
            return c.send({"t": "autherr", "x": "That username is taken"})
        log("registered", name)
    else:
        rows = sql("SELECT name, salt, pw FROM users WHERE name=?", name)
        if not rows or not hmac.compare_digest(rows[0][2], hash_pw(pw, rows[0][1])):
            time.sleep(1)  # slows down password guessing
            return c.send({"t": "autherr", "x": "Wrong username or password"})
        name = rows[0][0]
    with LOCK:
        old = online.get(name)
        if old is not None and old is not c:
            old.send({"t": "kick", "x": "You logged in from another place."})
            old.close(flush=True)
        online[name] = c
        c.user = name
        c.sock.settimeout(None)
        c.send({"t": "hello", "me": name})
        snapshot(c)
        tell_friends(name, {"t": "presence", "n": name, "on": True})
    log("login", name)


def logout(c):
    c.close()
    with LOCK:
        leave_room(c)
        if c.user and online.get(c.user) is c:
            del online[c.user]
            tell_friends(c.user, {"t": "presence", "n": c.user, "on": False})
            log("logout", c.user)


# ---------- friends ----------
def tell_friends(user, m):
    for (f,) in sql("SELECT b FROM friends WHERE a=?", user):
        if f in online:
            online[f].send(m)


def snapshot(c):  # friend list + requests, sent whenever something about them changes
    u = c.user
    fr = [{"n": f, "on": f in online,
           "u": sql("SELECT COUNT(*) FROM dms WHERE src=? AND dst=? AND seen=0", f, u)[0][0]}
          for (f,) in sql("SELECT b FROM friends WHERE a=? ORDER BY b", u)]
    c.send({"t": "friends", "d": fr,
            "inc": [r[0] for r in sql("SELECT src FROM requests WHERE dst=?", u)],
            "out": [r[0] for r in sql("SELECT dst FROM requests WHERE src=?", u)]})


def refresh(*names):
    for n in names:
        if n in online:
            snapshot(online[n])


def make_friends(a, b):
    for x in (a, b):
        if sql("SELECT COUNT(*) FROM friends WHERE a=?", x)[0][0] >= MAX_FRIENDS:
            for n in (a, b):
                if n in online:
                    online[n].send({"t": "err", "x": "%s has too many friends already." % x})
            return
    changed("DELETE FROM requests WHERE (src=? AND dst=?) OR (src=? AND dst=?)", a, b, b, a)
    changed("INSERT OR IGNORE INTO friends VALUES(?,?)", a, b)
    changed("INSERT OR IGNORE INTO friends VALUES(?,?)", b, a)
    refresh(a, b)


def fr_add(c, m):
    me, other = c.user, canon(text(m, "n", 40))
    if not other:
        return c.send({"t": "err", "x": "No one is called that. Check the spelling."})
    if other == me:
        return c.send({"t": "err", "x": "That's you."})
    if are_friends(me, other):
        return c.send({"t": "err", "x": "You are already friends with %s." % other})
    if sql("SELECT 1 FROM requests WHERE src=? AND dst=?", me, other):
        return c.send({"t": "err", "x": "You already asked %s." % other})
    if sql("SELECT 1 FROM requests WHERE src=? AND dst=?", other, me):  # they asked you first: just accept
        return make_friends(me, other)
    if sql("SELECT COUNT(*) FROM requests WHERE src=?", me)[0][0] >= MAX_PENDING:
        return c.send({"t": "err", "x": "Too many pending requests."})
    changed("INSERT INTO requests VALUES(?,?)", me, other)
    c.send({"t": "info", "x": "Friend request sent to %s." % other})
    refresh(me, other)
    if other in online:
        online[other].send({"t": "info", "x": "%s wants to be your friend." % me})


def fr_ok(c, m):
    other = canon(text(m, "n", 40))
    if other and sql("SELECT 1 FROM requests WHERE src=? AND dst=?", other, c.user):
        make_friends(c.user, other)


def fr_no(c, m):
    other = canon(text(m, "n", 40))
    if other:
        changed("DELETE FROM requests WHERE src=? AND dst=?", other, c.user)
        refresh(c.user, other)


def fr_del(c, m):
    other = canon(text(m, "n", 40))
    if other:
        changed("DELETE FROM friends WHERE (a=? AND b=?) OR (a=? AND b=?)", c.user, other, other, c.user)
        refresh(c.user, other)


# ---------- direct messages ----------
def dm(c, m):
    other, x = canon(text(m, "n", 40)), text(m, "x", 1000).strip()
    if not x or not other:
        return
    if not are_friends(c.user, other):
        return c.send({"t": "err", "x": "You can only message friends."})
    ts = int(time.time())
    changed("INSERT INTO dms(src, dst, text, ts) VALUES(?,?,?,?)", c.user, other, x, ts)
    if other in online:
        online[other].send({"t": "dm", "f": c.user, "x": x, "ts": ts})


def seen(c, m):
    other = canon(text(m, "n", 40))
    if other:
        changed("UPDATE dms SET seen=1 WHERE src=? AND dst=? AND seen=0", other, c.user)


def hist(c, m):
    other = canon(text(m, "n", 40))
    if not other or not are_friends(c.user, other):
        return
    seen(c, m)
    rows = sql("SELECT src, text, ts FROM (SELECT * FROM dms WHERE (src=? AND dst=?) OR (src=? AND dst=?) "
               "ORDER BY id DESC LIMIT 100) ORDER BY id", c.user, other, other, c.user)
    c.send({"t": "hist", "n": other, "d": [list(r) for r in rows]})


# ---------- hangout rooms ----------
def invite(c, m):
    other = canon(text(m, "n", 40))
    if not other or not are_friends(c.user, other):
        return c.send({"t": "err", "x": "You can only invite friends."})
    o = online.get(other)
    if o is None:
        return c.send({"t": "err", "x": "%s is offline." % other})
    if c.room is None:
        rid = secrets.token_hex(4)
        r = rooms[rid] = Room(rid)
        join_room(c, r)
    r = c.room
    if o.room is r:
        return c.send({"t": "info", "x": "%s is already here." % other})
    if len(r.members) >= MAX_ROOM:
        return c.send({"t": "err", "x": "The hangout is full (%d/%d)." % (MAX_ROOM, MAX_ROOM)})
    r.invited.add(other)
    o.send({"t": "invite", "f": c.user, "room": r.id})
    c.send({"t": "info", "x": "Invited %s." % other})


def join(c, m):
    r = rooms.get(text(m, "room", 20))
    if r is None:
        return c.send({"t": "err", "x": "That hangout has ended."})
    if r is c.room:
        return
    if c.user not in r.invited:
        return c.send({"t": "err", "x": "You need an invite to join that hangout."})
    if len(r.members) >= MAX_ROOM:
        return c.send({"t": "err", "x": "The hangout is full."})
    join_room(c, r)


def leave(c, m):
    leave_room(c)


def room_chat(c, m):
    if c.room:
        c.room.send({"t": "chat", "f": c.user, "x": text(m, "x", 500)}, skip=c)


def ttt_move(c, m):
    r, i = c.room, m["i"]
    if r is None or type(i) is not int or not 0 <= i < 9:
        return
    p = r.names()
    mark = "X" if p[:1] == [c.user] else "O" if p[1:2] == [c.user] else ""
    if mark and mark == r.turn and not r.res and not r.board[i]:
        b = r.board
        b[i], r.turn = mark, "O" if mark == "X" else "X"
        w = next((b[x] for x, y, z in LINES if b[x] and b[x] == b[y] == b[z]), "")
        r.res = w or ("D" if all(b) else "")
        r.send(r.ttt())


def ttt_reset(c, m):
    if c.room:
        c.room.reset()
        c.room.send(c.room.ttt())


def rps_pick(c, m):
    if c.room and m["c"] in ("r", "p", "s"):
        c.room.picks.setdefault(c.user, m["c"])
        c.room.send(c.room.rps())


def rps_reset(c, m):
    if c.room:
        c.room.picks.clear()
        c.room.send(c.room.rps())


def media(c, m):  # voice and screen frames: relay to the others in the room
    r, t = c.room, m["t"]
    if r is None:
        return
    d = m.get("d", "")
    if not isinstance(d, str) or len(d) > 600_000:
        return
    r.send({"t": t, "f": c.user, "d": d}, skip=c, media=t != "scr_stop")


HANDLERS = {"fr_add": fr_add, "fr_ok": fr_ok, "fr_no": fr_no, "fr_del": fr_del, "dm": dm, "seen": seen,
            "hist": hist, "invite": invite, "join": join, "leave": leave, "chat": room_chat,
            "ttt_move": ttt_move, "ttt_reset": ttt_reset, "rps_pick": rps_pick, "rps_reset": rps_reset}


# ---------- connections ----------
def serve(sock):
    c = Conn(sock)
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        sock.settimeout(30)  # must log in within 30 seconds; lifted after login
        threading.Thread(target=c.writer, daemon=True).start()
        f = sock.makefile("rb")
        while True:
            line = f.readline(MAX_LINE)
            if not line.endswith(b"\n"):  # closed, or a line that is far too long
                break
            try:
                m = json.loads(line)
                t = m["t"]
            except (ValueError, KeyError, TypeError):
                break
            try:
                if c.user is None:
                    if t in ("login", "register"):
                        auth(c, m)
                    else:
                        break
                elif t in MEDIA:
                    media(c, m)
                elif t in HANDLERS:
                    with LOCK:
                        HANDLERS[t](c, m)
            except (KeyError, TypeError, ValueError, IndexError, AttributeError):
                pass  # a malformed message is ignored
    except OSError:
        pass
    finally:
        logout(c)


def main():
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("", PORT))
    s.listen(64)
    log("Hangout server listening on port %d (database: %s)" % (PORT, DB_FILE))
    while True:
        sock, addr = s.accept()
        if threading.active_count() > MAX_THREADS:
            sock.close()
            continue
        threading.Thread(target=serve, args=(sock,), daemon=True).start()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
