#!/usr/bin/env python3
"""Hangout: friends, messages, voice chat, screen sharing and games (up to 5 people per hangout).
Log in, add friends by username, message them, or invite them into a hangout.
Needs the Hangout server (server.py) running somewhere everyone can reach."""
import array, base64, io, json, os, queue, socket, threading, time, tkinter as tk
from tkinter import messagebox

try:
    import sounddevice as sd  # voice chat
except Exception:
    sd = None
try:
    from PIL import Image, ImageGrab, ImageTk  # screen sharing
except Exception:
    Image = None

DEFAULT_SERVER = "127.0.0.1"  # <- put your public server address here before you share the app
PORT, MAX = 5000, 5
CONF = os.path.join(os.path.expanduser("~"), ".hangout.json")  # remembers server + username (never the password)
RATE, BLOCK = 16000, 640  # 16 kHz mono, 40 ms per voice packet
BG, BG2, FG, ACC = "#313338", "#2b2d31", "#f2f3f5", "#5865f2"
GREEN, GRAY, RED = "#23a55a", "#80848e", "#ed4245"
RPS = {"r": "Rock", "p": "Paper", "s": "Scissors"}
BEATS = {"r": "s", "s": "p", "p": "r"}
ROOM_MSGS = ("names", "chat", "v", "scr", "scr_stop", "ttt_state", "rps_state")


def btn(p, text, cmd, bg=ACC, **kw):
    return tk.Button(p, text=text, command=cmd, bg=bg, fg="white", relief="flat", padx=8, pady=4, **kw)


def head(p, text):
    return tk.Label(p, text=text, font=("Arial", 12, "bold"), bg=BG2, fg=FG)


def load_conf():
    try:
        with open(CONF, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_conf(d):
    try:
        with open(CONF, "w", encoding="utf-8") as f:
            json.dump(d, f)
    except OSError:
        pass


def split_addr(a):  # "example.com" or "example.com:6000"
    host, sep, port = a.strip().rpartition(":")
    return (host, int(port)) if sep and port.isdigit() else (a.strip(), PORT)


def stamp(ts):  # 14:05 for today, 03.11 14:05 for older messages
    t = time.localtime(ts)
    return time.strftime("%H:%M" if time.strftime("%Y%j") == time.strftime("%Y%j", t) else "%d.%m %H:%M", t)


class App:
    def __init__(self, root):
        self.root, self.q = root, queue.Queue()
        conf = load_conf()
        self.srv = tk.StringVar(value=conf.get("server", DEFAULT_SERVER))
        self.user = tk.StringVar(value=conf.get("user", ""))
        self.pw = tk.StringVar()
        self.sock = self.outq = None
        self.busy, self.me, self.note_id = False, "", None
        self.friends, self.inc, self.pending, self.fnames = {}, [], [], []  # friends: name -> {"on", "u"nread}
        self.cur, self.pane, self.invites = None, "dm", {}  # open chat, visible tab, pending hangout invites
        self.in_room, self.room_names = False, []
        self.inp = self.out = None
        self.muted = self.sharing = False
        self.vq, self.view = {}, {}
        self.board, self.turn, self.res = [""] * 9, "X", ""
        root.title("Hangout")
        root.configure(bg=BG)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.login_screen()
        root.after(20, self.poll)
        root.after(25000, self.ping)

    # ---------- login ----------
    def clear(self):
        for c in self.root.winfo_children():
            c.destroy()

    def login_screen(self, msg=""):
        self.clear()
        self.root.minsize(1, 1)
        self.root.geometry("")
        f = tk.Frame(self.root, bg=BG, padx=30, pady=24)
        f.pack()
        tk.Label(f, text="Hangout", font=("Arial", 22, "bold"), bg=BG, fg=FG).pack(pady=(0, 12))
        for label, var, show in (("Server", self.srv, ""), ("Username", self.user, ""), ("Password", self.pw, "•")):
            tk.Label(f, text=label, bg=BG, fg=FG).pack()
            e = tk.Entry(f, textvariable=var, justify="center", show=show, width=26)
            e.pack(pady=(0, 8))
        e.bind("<Return>", lambda _: self.connect(False))  # e is the password box here
        row = tk.Frame(f, bg=BG)
        row.pack(pady=(6, 0))
        btn(row, "Log in", lambda: self.connect(False)).pack(side="left", padx=4)
        btn(row, "Create account", lambda: self.connect(True), bg=BG2).pack(side="left", padx=4)
        self.status = tk.Label(f, text=msg, bg=BG, fg=FG, wraplength=260)
        self.status.pack(pady=10)

    def connect(self, register):
        user, pw = self.user.get().strip(), self.pw.get()
        if self.busy:
            return
        if not user or not pw:
            self.status.config(text="Type a username and a password.")
            return
        host, port = split_addr(self.srv.get())
        self.busy = True
        self.status.config(text="Connecting…")

        def run():
            try:
                s = socket.create_connection((host, port), timeout=8)
                s.settimeout(None)
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            except OSError as e:
                self.q.put((None, {"t": "neterr", "x": str(e)}))
                return
            self.outq = queue.Queue()
            threading.Thread(target=self.writer, args=(s, self.outq), daemon=True).start()
            self.sock = s
            self.send({"t": "register" if register else "login", "n": user, "p": pw})
            self.read(s)

        threading.Thread(target=run, daemon=True).start()

    def logout(self, msg=""):
        if self.in_room:
            self.leave_room(tell=False)
        self.hangup()
        self.busy, self.me, self.cur = False, "", None
        self.friends, self.inc, self.pending, self.invites = {}, [], [], {}
        self.login_screen(msg)

    def close(self):
        if self.in_room:
            self.leave_room(tell=False)
        self.hangup()
        self.root.destroy()

    # ---------- networking ----------
    def read(self, s):
        try:
            for line in s.makefile("r", encoding="utf-8"):
                self.q.put((s, json.loads(line)))
        except (OSError, ValueError):
            pass
        self.q.put((s, {"t": "bye"}))

    def writer(self, s, oq):  # sends in the background so voice and screen never freeze the window
        while True:
            data = oq.get()
            if data is None:
                break
            try:
                s.sendall(data)
            except OSError:
                break

    def send(self, m, media=False):  # any thread
        q = self.outq
        if self.sock is None or q is None or (media and q.qsize() > 30):  # drop media if the line is slow
            return
        q.put((json.dumps(m, separators=(",", ":")) + "\n").encode())

    def hangup(self):
        s, q, self.sock, self.outq = self.sock, self.outq, None, None
        if q:
            q.put(None)
        if s:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            s.close()

    def ping(self):  # keeps the connection alive through routers
        self.send({"t": "ping"})
        self.root.after(25000, self.ping)

    def poll(self):
        self.root.after(20, self.poll)
        msgs = []
        while True:
            try:
                msgs.append(self.q.get_nowait())
            except queue.Empty:
                break
        latest, rest = {}, []  # of the screen frames that piled up, only show the newest per person
        for s, m in msgs:
            if m.get("t") == "scr":
                latest[m.get("f")] = (s, m)
            else:
                rest.append((s, m))
        for s, m in rest + list(latest.values()):
            try:
                self.inbox(s, m)
            except (KeyError, ValueError, TypeError, IndexError):
                pass

    def inbox(self, s, m):
        t = m.get("t")
        if t == "neterr":
            self.busy = False
            self.status.config(text="Could not connect: " + m["x"])
            return
        if s is not self.sock:  # leftover from a connection we already closed
            return
        if t == "hello":
            self.busy, self.me = False, m["me"]
            self.user.set(self.me)
            self.pw.set("")
            save_conf({"server": self.srv.get().strip(), "user": self.me})
            self.home_screen()
        elif t == "autherr":
            self.busy = False
            self.hangup()
            self.status.config(text=m["x"])
        elif t == "kick":
            self.logout(m["x"])
        elif t == "bye":
            self.logout("Lost the connection to the server.")
        elif t in ("err", "info"):
            self.note(m["x"])
        elif t == "friends":
            self.friends = {f["n"]: {"on": f["on"], "u": f["u"]} for f in m["d"]}
            self.inc, self.pending = m["inc"], m["out"]
            if self.cur and self.cur not in self.friends:
                self.cur = None
                self.dm_empty()
            self.dm_head()
            self.draw_friends()
            self.draw_requests()
        elif t == "presence":
            if m["n"] in self.friends:
                self.friends[m["n"]]["on"] = m["on"]
                self.draw_friends()
                if m["n"] == self.cur:
                    self.dm_head()
        elif t == "dm":
            n = m["f"]
            if n == self.cur and self.pane == "dm":
                self.put(self.dm_log, "%s  %s: %s" % (stamp(m["ts"]), n, m["x"]))
                self.send({"t": "seen", "n": n})
            else:
                if n in self.friends:
                    self.friends[n]["u"] += 1
                self.draw_friends()
                self.note("New message from %s" % n)
                self.root.bell()
        elif t == "hist":
            if m["n"] == self.cur:
                self.clear_text(self.dm_log)
                if not m["d"]:
                    self.put(self.dm_log, "No messages yet. Say hi!", "sys")
                for who, x, ts in m["d"]:
                    self.put(self.dm_log, "%s  %s: %s" % (stamp(ts), who, x), "me" if who == self.me else "")
        elif t == "invite":
            self.invites[m["room"]] = m["f"]
            self.draw_invites()
            self.root.bell()
        elif t == "room":  # the server put us in a hangout
            if self.in_room:
                self.leave_room(tell=False)
            self.enter_room()
        elif t in ROOM_MSGS and self.in_room:
            self.show(m)

    # ---------- home screen ----------
    def home_screen(self):
        self.clear()
        r = self.root
        r.geometry("1080x740")
        r.minsize(900, 600)
        r.columnconfigure(1, weight=1)
        r.rowconfigure(0, weight=1)
        self.build_side()
        main = tk.Frame(r, bg=BG)
        main.grid(row=0, column=1, sticky="nsew")
        bar = tk.Frame(main, bg=BG2, padx=8, pady=6)
        bar.pack(fill="x")
        self.tab_dm = btn(bar, "Messages", lambda: self.show_pane("dm"))
        self.tab_dm.pack(side="left", padx=2)
        self.tab_room = btn(bar, "Hangout", lambda: self.show_pane("room"))  # only shown while you are in one
        self.inv_box = tk.Frame(main, bg=BG)
        self.inv_box.pack(fill="x")
        self.dm_pane = self.build_dm(main)
        self.room_pane = self.build_room(main)
        self.in_room = False
        self.show_pane("dm")
        self.draw_requests()
        self.draw_friends()

    def build_side(self):
        s = tk.Frame(self.root, bg=BG2, padx=12, pady=12, width=250)
        s.grid(row=0, column=0, sticky="ns")
        s.pack_propagate(False)
        tk.Label(s, text="Hangout", font=("Arial", 16, "bold"), bg=BG2, fg=FG).pack(anchor="w")
        me = tk.Frame(s, bg=BG2)
        me.pack(fill="x", pady=(2, 12))
        tk.Label(me, text=self.me, bg=BG2, fg=FG).pack(side="left")
        btn(me, "Log out", self.logout, bg=BG).pack(side="right")
        head(s, "Add a friend").pack(anchor="w")
        row = tk.Frame(s, bg=BG2)
        row.pack(fill="x", pady=4)
        self.add = tk.Entry(row)
        self.add.pack(side="left", fill="x", expand=True)
        self.add.bind("<Return>", self.add_friend)
        btn(row, "Add", self.add_friend).pack(side="left", padx=(4, 0))
        self.req_box = tk.Frame(s, bg=BG2)
        self.req_box.pack(fill="x")
        head(s, "Friends").pack(anchor="w", pady=(10, 2))
        self.flist = tk.Listbox(s, bg=BG2, fg=FG, bd=0, highlightthickness=0, activestyle="none",
                                exportselection=False, selectbackground=ACC, selectforeground="white",
                                font=("Arial", 11))
        self.flist.pack(fill="both", expand=True)
        self.flist.bind("<<ListboxSelect>>", self.pick_friend)
        self.note_lbl = tk.Label(s, bg=BG2, fg=FG, wraplength=220, justify="left")
        self.note_lbl.pack(fill="x", pady=(8, 0))

    def build_dm(self, p):
        f = tk.Frame(p, bg=BG)
        top = tk.Frame(f, bg=BG, padx=10, pady=8)
        top.pack(fill="x")
        self.dm_title = tk.Label(top, font=("Arial", 13, "bold"), bg=BG, fg=FG)
        self.dm_title.pack(side="left")
        self.dm_btns = tk.Frame(top, bg=BG)  # shown once a friend is selected
        btn(self.dm_btns, "Invite to hangout", self.invite_cur, bg=GREEN).pack(side="left", padx=2)
        btn(self.dm_btns, "Remove friend", self.remove_cur, bg=BG2).pack(side="left", padx=2)
        bottom = tk.Frame(f, bg=BG)
        bottom.pack(side="bottom", fill="x")
        self.dm_msg = tk.Entry(bottom)
        self.dm_msg.pack(side="left", fill="x", expand=True, padx=8, pady=8)
        self.dm_msg.bind("<Return>", self.dm_say)
        btn(bottom, "Send", self.dm_say).pack(side="right", padx=(0, 8))
        self.dm_log = tk.Text(f, bg=BG, fg=FG, state="disabled", wrap="word", bd=0, padx=8, pady=8)
        self.dm_log.pack(fill="both", expand=True)
        self.dm_log.tag_config("me", foreground="#a5b4ff")
        self.dm_log.tag_config("sys", foreground=GRAY)
        self.dm_empty()
        self.dm_head()
        return f

    def build_room(self, p):
        f = tk.Frame(p, bg=BG)
        f.columnconfigure(0, weight=1)
        f.rowconfigure(0, weight=1)
        left = tk.Frame(f, bg=BG)
        left.grid(row=0, column=0, sticky="nsew")
        left.rowconfigure(0, weight=1)
        left.columnconfigure(0, weight=1)
        self.chat = tk.Text(left, bg=BG, fg=FG, state="disabled", wrap="word", bd=0, padx=8, pady=8)
        self.chat.grid(row=0, column=0, columnspan=2, sticky="nsew")
        self.chat.tag_config("me", foreground="#a5b4ff")
        self.chat.tag_config("sys", foreground=GRAY)
        self.msg = tk.Entry(left)
        self.msg.grid(row=1, column=0, sticky="ew", padx=8, pady=8)
        self.msg.bind("<Return>", self.say)
        btn(left, "Send", self.say).grid(row=1, column=1, padx=(0, 8))

        r = tk.Frame(f, bg=BG2, padx=14, pady=10)
        r.grid(row=0, column=1, sticky="ns")
        btn(r, "Leave hangout", self.leave_room, bg=RED).pack(fill="x", pady=(0, 6))
        mb = tk.Menubutton(r, text="Invite a friend", bg=ACC, fg="white", relief="flat", padx=8, pady=4,
                           activebackground=ACC, activeforeground="white")
        self.inv_menu = tk.Menu(mb, tearoff=0, postcommand=self.fill_invite_menu)
        mb.config(menu=self.inv_menu)
        mb.pack(fill="x", pady=(0, 8))
        self.ppl = tk.Label(r, bg=BG2, fg=FG, wraplength=230, justify="left")
        self.ppl.pack(pady=(0, 8))
        head(r, "Voice chat").pack()
        self.vbtn = btn(r, "Start voice", self.voice_toggle)
        self.vbtn.pack(pady=4)
        self.mbtn = btn(r, "Mute mic", self.mute_toggle, bg=BG)
        self.mbtn.pack()
        self.vlbl = tk.Label(r, bg=BG2, fg=FG, wraplength=230)
        self.vlbl.pack(pady=(4, 8))
        head(r, "Screen sharing").pack()
        self.sbtn = btn(r, "Share screen", self.share_toggle)
        self.sbtn.pack(pady=(4, 8))
        head(r, "Tic-Tac-Toe").pack()
        grid = tk.Frame(r, bg=BG2)
        grid.pack(pady=4)
        self.cells = []
        for i in range(9):
            b = btn(grid, "", lambda i=i: self.send({"t": "ttt_move", "i": i}), bg=BG, font=("Arial", 16), width=3)
            b.grid(row=i // 3, column=i % 3, padx=2, pady=2)
            self.cells.append(b)
        self.ttt_lbl = tk.Label(r, bg=BG2, fg=FG, wraplength=230)
        self.ttt_lbl.pack()
        btn(r, "New game", lambda: self.send({"t": "ttt_reset"})).pack(pady=4)
        head(r, "Rock Paper Scissors").pack(pady=(10, 4))
        row = tk.Frame(r, bg=BG2)
        row.pack()
        for k, v in RPS.items():
            btn(row, v, lambda k=k: self.send({"t": "rps_pick", "c": k})).pack(side="left", padx=2)
        self.rps_lbl = tk.Label(r, bg=BG2, fg=FG, wraplength=230)
        self.rps_lbl.pack(pady=4)
        btn(r, "New round", lambda: self.send({"t": "rps_reset"})).pack()
        return f

    def show_pane(self, which):
        if which == "room" and not self.in_room:
            which = "dm"
        self.pane = which
        self.dm_pane.pack_forget()
        self.room_pane.pack_forget()
        (self.room_pane if which == "room" else self.dm_pane).pack(fill="both", expand=True)
        self.tab_dm.config(bg=ACC if which == "dm" else BG)
        self.tab_room.config(bg=ACC if which == "room" else BG)
        if which == "dm" and self.cur in self.friends and self.friends[self.cur]["u"]:
            self.friends[self.cur]["u"] = 0  # messages that came in while you were in the hangout
            self.draw_friends()
            self.send({"t": "hist", "n": self.cur})

    def note(self, text):  # short message at the bottom of the sidebar
        self.note_lbl.config(text=text)
        if self.note_id:
            self.root.after_cancel(self.note_id)
        self.note_id = self.root.after(7000, self.clear_note)

    def clear_note(self):
        try:
            self.note_lbl.config(text="")
        except tk.TclError:  # the sidebar is gone (logged out)
            pass

    @staticmethod
    def put(w, text, tag=""):
        w.config(state="normal")
        w.insert("end", text + "\n", tag)
        w.config(state="disabled")
        w.see("end")

    @staticmethod
    def clear_text(w):
        w.config(state="normal")
        w.delete("1.0", "end")
        w.config(state="disabled")

    # ---------- friends ----------
    def add_friend(self, _=None):
        n = self.add.get().strip()
        if n:
            self.add.delete(0, "end")
            self.send({"t": "fr_add", "n": n})

    def draw_friends(self):
        self.fnames = sorted(self.friends, key=lambda n: (not self.friends[n]["on"], n.lower()))
        self.flist.delete(0, "end")
        for i, n in enumerate(self.fnames):
            f = self.friends[n]
            self.flist.insert("end", "%s %s%s" % ("●" if f["on"] else "○", n, "   (%d new)" % f["u"] if f["u"] else ""))
            self.flist.itemconfig(i, fg="white" if f["u"] else GREEN if f["on"] else GRAY)
        if self.cur in self.fnames:
            self.flist.selection_set(self.fnames.index(self.cur))

    def draw_requests(self):
        for w in self.req_box.winfo_children():
            w.destroy()
        if self.inc:
            head(self.req_box, "Friend requests").pack(anchor="w", pady=(8, 2))
        for n in self.inc:
            row = tk.Frame(self.req_box, bg=BG2)
            row.pack(fill="x", pady=1)
            tk.Label(row, text=n, bg=BG2, fg=FG).pack(side="left")
            btn(row, "✕", lambda n=n: self.send({"t": "fr_no", "n": n}), bg=BG).pack(side="right", padx=(2, 0))
            btn(row, "Accept", lambda n=n: self.send({"t": "fr_ok", "n": n}), bg=GREEN).pack(side="right")
        for n in self.pending:
            tk.Label(self.req_box, text="Waiting for %s to accept" % n, bg=BG2, fg=GRAY).pack(anchor="w")

    def pick_friend(self, _=None):
        sel = self.flist.curselection()
        if sel and sel[0] < len(self.fnames):
            self.open_dm(self.fnames[sel[0]])

    def open_dm(self, n):
        self.cur = n
        self.friends[n]["u"] = 0
        self.clear_text(self.dm_log)
        self.dm_head()
        self.draw_friends()
        self.show_pane("dm")
        self.dm_msg.focus_set()
        self.send({"t": "hist", "n": n})  # the server also marks these messages as read

    def dm_head(self):
        if self.cur in self.friends:
            self.dm_title.config(text="%s  (%s)" % (self.cur, "online" if self.friends[self.cur]["on"] else "offline"))
            self.dm_btns.pack(side="right")
        else:
            self.dm_title.config(text="Messages")
            self.dm_btns.pack_forget()

    def dm_empty(self):
        self.clear_text(self.dm_log)
        self.put(self.dm_log, "Add a friend by username on the left. Once they accept, pick them in the list to "
                              "message them, or invite them to a hangout with voice, screen sharing and games.", "sys")

    def dm_say(self, _=None):
        x = self.dm_msg.get().strip()
        if x and self.cur:
            self.dm_msg.delete(0, "end")
            self.put(self.dm_log, "%s  %s: %s" % (stamp(time.time()), self.me, x), "me")
            self.send({"t": "dm", "n": self.cur, "x": x})

    def invite_cur(self):
        if self.cur:
            self.send({"t": "invite", "n": self.cur})

    def remove_cur(self):
        if self.cur and messagebox.askyesno("Hangout", "Remove %s from your friends?" % self.cur):
            self.send({"t": "fr_del", "n": self.cur})

    # ---------- hangout invites and rooms ----------
    def draw_invites(self):
        for w in self.inv_box.winfo_children():
            w.destroy()
        for rid, who in self.invites.items():
            row = tk.Frame(self.inv_box, bg=ACC, padx=10, pady=6)
            row.pack(fill="x")
            tk.Label(row, text="%s invited you to a hangout" % who, bg=ACC, fg="white").pack(side="left")
            btn(row, "Dismiss", lambda r=rid: self.drop_invite(r), bg=BG2).pack(side="right", padx=2)
            btn(row, "Join", lambda r=rid: self.join(r), bg=GREEN).pack(side="right", padx=2)

    def drop_invite(self, rid):
        self.invites.pop(rid, None)
        self.draw_invites()

    def join(self, rid):
        self.send({"t": "join", "room": rid})
        self.drop_invite(rid)

    def fill_invite_menu(self):  # friends who are online and not in this hangout yet
        self.inv_menu.delete(0, "end")
        free = sorted(n for n, f in self.friends.items() if f["on"] and n not in self.room_names)
        for n in free:
            self.inv_menu.add_command(label=n, command=lambda n=n: self.send({"t": "invite", "n": n}))
        if not free:
            self.inv_menu.add_command(label="No friends are online", state="disabled")

    def enter_room(self):
        self.in_room, self.room_names = True, []
        self.board, self.turn, self.res = [""] * 9, "X", ""
        self.clear_text(self.chat)
        self.ppl.config(text="")
        self.ttt_lbl.config(text="")
        self.rps_lbl.config(text="")
        for c in self.cells:
            c.config(text="")
        self.vlbl.config(text="Voice off")
        self.tab_room.pack(side="left", padx=2)
        self.show_pane("room")
        self.msg.focus_set()

    def leave_room(self, tell=True):
        if tell:
            self.send({"t": "leave"})
        self.voice_stop()
        self.sharing = False
        self.sbtn.config(text="Share screen")
        for f in list(self.view):
            self.close_view(f)
        self.in_room, self.room_names = False, []
        self.tab_room.pack_forget()
        self.show_pane("dm")

    # ---------- showing what arrives in the hangout ----------
    def show(self, m):
        t = m["t"]
        if t == "names":
            new = m["d"]
            if self.room_names:
                for n in new:
                    if n not in self.room_names and n != self.me:
                        self.log("%s joined" % n, "sys")
                for n in self.room_names:
                    if n not in new:
                        self.log("%s left" % n, "sys")
                        self.vq.pop(n, None)
                        self.close_view(n)
            self.room_names = new
            self.ppl.config(text="In the hangout (%d/%d): %s" % (len(new), MAX, ", ".join(new)))
        elif t == "chat":
            self.log("%s: %s" % (m["f"], m["x"]))
        elif t == "v":
            q = self.vq.setdefault(m["f"], queue.Queue())
            if self.out and q.qsize() < 10:
                q.put(base64.b64decode(m["d"]))
        elif t == "scr":
            self.show_screen(m["f"], m["d"])
        elif t == "scr_stop":
            self.close_view(m["f"])
        elif t == "ttt_state":
            self.board, self.turn, self.res = m["b"], m["turn"], m["w"]
            mk = next((k for k, v in m["p"].items() if v == self.me), "")
            for c, v in zip(self.cells, self.board):
                c.config(text=v)
            if self.res:
                s = "Draw" if self.res == "D" else "You win!" if self.res == mk else "%s wins" % self.res
            elif not mk:
                s = "Watching"
            elif not m["p"]["O"]:
                s = "Waiting for a second player…"
            else:
                s = "Your turn (%s)" % mk if self.turn == mk else "Waiting…"
            self.ttt_lbl.config(text=s)
        elif t == "rps_state":
            r = m["r"]
            if r:
                s = set(r.values())
                if len(s) == 2:
                    a, b = s
                    w = a if BEATS[a] == b else b
                    res = "Winner: " + ", ".join(k for k, c in r.items() if c == w)
                else:
                    res = "Tie"
                txt = ", ".join("%s: %s" % (k, RPS[c]) for k, c in r.items()) + "\n" + res
            else:
                txt = "Picked %d of %d. %s" % (len(m["p"]), m["n"], "Waiting…" if self.me in m["p"] else "Pick one")
            self.rps_lbl.config(text=txt)

    def log(self, text, tag=""):
        self.put(self.chat, text, tag)

    def say(self, _=None):
        x = self.msg.get().strip()
        if not x:
            return
        self.msg.delete(0, "end")
        self.log("%s: %s" % (self.me, x), "me")
        self.send({"t": "chat", "x": x})

    # ---------- voice ----------
    def voice_toggle(self):
        if self.inp:
            return self.voice_stop()
        if sd is None:
            self.vlbl.config(text="Voice needs the sounddevice package (pip install sounddevice)")
            return
        try:
            self.out = sd.RawOutputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=BLOCK, callback=self.speaker)
            self.inp = sd.RawInputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=BLOCK, callback=self.mic)
            self.out.start()
            self.inp.start()
        except Exception as e:
            self.voice_stop()
            self.vlbl.config(text="Mic or speaker error: %s" % e)
            return
        self.vbtn.config(text="Stop voice")
        self.vlbl.config(text="Voice on. Use headphones to avoid echo.")

    def voice_stop(self):
        for s in (self.inp, self.out):
            if s:
                try:
                    s.stop()
                    s.close()
                except Exception:
                    pass
        self.inp = self.out = None
        self.vq.clear()
        self.vbtn.config(text="Start voice")
        self.vlbl.config(text="Voice off")

    def mute_toggle(self, _=None):
        self.muted = not self.muted
        self.mbtn.config(text="Unmute mic" if self.muted else "Mute mic")

    def mic(self, data, frames, _time, status):
        if not self.muted:
            self.send({"t": "v", "d": base64.b64encode(bytes(data)).decode()}, media=True)

    def speaker(self, outdata, frames, _time, status):  # mixes everyone who is talking
        mix = [0] * BLOCK
        for q in list(self.vq.values()):
            try:
                a = array.array("h", q.get_nowait())
            except queue.Empty:
                continue
            if len(a) == BLOCK:
                for i, s in enumerate(a):
                    mix[i] += s
        outdata[:] = array.array("h", [max(-32768, min(32767, s)) for s in mix]).tobytes()

    # ---------- screen sharing ----------
    def share_toggle(self):
        if self.sharing:
            self.sharing = False
            self.sbtn.config(text="Share screen")
        elif Image is None:
            self.log("Screen sharing needs Pillow (pip install pillow)", "sys")
        else:
            self.sharing = True
            self.sbtn.config(text="Stop sharing")
            self.log("Sharing your screen (~25 FPS)", "sys")
            threading.Thread(target=self.grab_loop, daemon=True).start()

    def grab_loop(self):
        target_fps = 25
        interval = 1.0 / target_fps
        while self.sharing:
            start_time = time.time()
            try:
                img = ImageGrab.grab()
                img.thumbnail((720, 405))
                buf = io.BytesIO()
                img.convert("RGB").save(buf, "JPEG", quality=45, optimize=True)
                self.send({"t": "scr", "d": base64.b64encode(buf.getvalue()).decode()}, media=True)
            except Exception:
                self.sharing = False
                break
            elapsed = time.time() - start_time
            if elapsed < interval:
                time.sleep(interval - elapsed)
        self.send({"t": "scr_stop"})

    def show_screen(self, f, data):
        w = self.view.get(f)
        if Image is None or w == "x":
            return
        img = ImageTk.PhotoImage(Image.open(io.BytesIO(base64.b64decode(data))))
        if not w:
            top = tk.Toplevel(self.root)
            top.title("Screen: " + f)
            top.protocol("WM_DELETE_WINDOW", lambda: self.close_view(f, True))
            w = self.view[f] = (top, tk.Label(top))
            w[1].pack()
        w[1].config(image=img)
        w[1].image = img

    def close_view(self, f, hide=False):
        w = self.view.pop(f, None)
        if isinstance(w, tuple):
            w[0].destroy()
        if hide:
            self.view[f] = "x"  # ignore frames until they stop and start sharing again


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
