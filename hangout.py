#!/usr/bin/env python3
"""Hangout: chat and play games with a friend. Needs only Python 3 (tkinter included).
One person clicks Host, the other types the host's address and clicks Join."""
import base64, json, queue, socket, threading, tkinter as tk

try:
    import sounddevice as sd  # voice chat; the app still works without it
except Exception:
    sd = None

PORT = 5000
RATE, BLOCK = 16000, 640  # 16 kHz mono, 40 ms per packet
BG, BG2, FG, ACC = "#313338", "#2b2d31", "#f2f3f5", "#5865f2"
LINES = [(0, 1, 2), (3, 4, 5), (6, 7, 8), (0, 3, 6), (1, 4, 7), (2, 5, 8), (0, 4, 8), (2, 4, 6)]
RPS = {"r": "Rock", "p": "Paper", "s": "Scissors"}
BEATS = {"r": "s", "s": "p", "p": "r"}


def local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "unknown"


def btn(p, text, cmd, bg=ACC, **kw):
    return tk.Button(p, text=text, command=cmd, bg=bg, fg="white", relief="flat", padx=8, pady=4, **kw)


class App:
    def __init__(self, root):
        self.root, self.sock, self.q = root, None, queue.Queue()
        self.name, self.ip = tk.StringVar(value="Me"), tk.StringVar()
        self.inp = self.out = None
        self.muted = False
        self.play, self.lock = queue.Queue(), threading.Lock()
        root.title("Hangout")
        root.configure(bg=BG)
        self.connect_screen()
        root.after(100, self.poll)

    # ---------- screens ----------
    def clear(self):
        for c in self.root.winfo_children():
            c.destroy()

    def connect_screen(self):
        self.clear()
        f = tk.Frame(self.root, bg=BG, padx=30, pady=24)
        f.pack()
        tk.Label(f, text="Hangout", font=("Arial", 22, "bold"), bg=BG, fg=FG).pack(pady=(0, 12))
        tk.Label(f, text="Your name", bg=BG, fg=FG).pack()
        tk.Entry(f, textvariable=self.name, justify="center").pack(pady=4)
        btn(f, "Host (your friend joins you)", self.host).pack(pady=(14, 4))
        tk.Label(f, text="or join a friend's address", bg=BG, fg=FG).pack(pady=(10, 0))
        tk.Entry(f, textvariable=self.ip, justify="center").pack(pady=4)
        btn(f, "Join", self.join).pack()
        self.status = tk.Label(f, text="", bg=BG, fg=FG, wraplength=260)
        self.status.pack(pady=10)

    def main_screen(self):
        self.clear()
        self.root.geometry("780x460")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        left = tk.Frame(self.root, bg=BG)
        left.grid(row=0, column=0, sticky="nsew")
        left.rowconfigure(0, weight=1)
        left.columnconfigure(0, weight=1)
        self.chat = tk.Text(left, bg=BG, fg=FG, state="disabled", wrap="word", bd=0, padx=8, pady=8)
        self.chat.grid(row=0, column=0, columnspan=2, sticky="nsew")
        self.chat.tag_config("me", foreground="#a5b4ff")
        self.chat.tag_config("sys", foreground="#80848e")
        self.msg = tk.Entry(left)
        self.msg.grid(row=1, column=0, sticky="ew", padx=8, pady=8)
        self.msg.bind("<Return>", self.say)
        self.msg.focus_set()
        btn(left, "Send", self.say).grid(row=1, column=1, padx=(0, 8))

        right = tk.Frame(self.root, bg=BG2, padx=14, pady=10)
        right.grid(row=0, column=1, sticky="ns")
        tk.Label(right, text="Voice chat", font=("Arial", 13, "bold"), bg=BG2, fg=FG).pack()
        self.vbtn = btn(right, "Start voice", self.voice_toggle)
        self.vbtn.pack(pady=4)
        self.mbtn = btn(right, "Mute mic", self.mute_toggle, bg=BG)
        self.mbtn.pack()
        self.vlbl = tk.Label(right, bg=BG2, fg=FG, wraplength=220)
        self.vlbl.pack(pady=(4, 14))
        tk.Label(right, text="Tic-Tac-Toe", font=("Arial", 13, "bold"), bg=BG2, fg=FG).pack()
        grid = tk.Frame(right, bg=BG2)
        grid.pack(pady=6)
        self.cells = []
        for i in range(9):
            b = btn(grid, "", lambda i=i: self.ttt_click(i), bg=BG, font=("Arial", 20), width=3)
            b.grid(row=i // 3, column=i % 3, padx=2, pady=2)
            self.cells.append(b)
        self.ttt_lbl = tk.Label(right, bg=BG2, fg=FG)
        self.ttt_lbl.pack()
        btn(right, "New game", self.ttt_new).pack(pady=4)
        tk.Label(right, text="Rock Paper Scissors", font=("Arial", 13, "bold"), bg=BG2, fg=FG).pack(pady=(16, 4))
        row = tk.Frame(right, bg=BG2)
        row.pack()
        for k, v in RPS.items():
            btn(row, v, lambda k=k: self.rps_pick(k)).pack(side="left", padx=2)
        self.rps_lbl = tk.Label(right, bg=BG2, fg=FG, wraplength=220)
        self.rps_lbl.pack(pady=4)
        btn(right, "New round", self.rps_new).pack()
        self.ttt_reset()
        self.rps_reset()

    # ---------- networking ----------
    def host(self):
        self.status.config(text="Tell your friend to join: %s\nWaiting for them…" % local_ip())

        def run():
            try:
                s = socket.socket()
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(("", PORT))
                s.listen(1)
                c, _ = s.accept()
                s.close()
                self.start(c, "X")
            except OSError as e:
                self.q.put({"t": "err", "x": str(e)})

        threading.Thread(target=run, daemon=True).start()

    def join(self):
        addr = self.ip.get().strip()
        if not addr:
            self.status.config(text="Type your friend's address first.")
            return
        self.status.config(text="Connecting…")

        def run():
            try:
                c = socket.create_connection((addr, PORT), timeout=8)
                c.settimeout(None)
                self.start(c, "O")
            except OSError as e:
                self.q.put({"t": "err", "x": str(e)})

        threading.Thread(target=run, daemon=True).start()

    def start(self, c, mark):
        self.sock, self.me = c, mark
        self.q.put({"t": "up"})
        try:
            for line in c.makefile("r", encoding="utf-8"):
                self.q.put(json.loads(line))
        except (OSError, ValueError):
            pass
        self.q.put({"t": "bye"})

    def send(self, quiet=False, **m):
        try:
            with self.lock:
                self.sock.sendall((json.dumps(m) + "\n").encode())
        except (OSError, AttributeError):
            if not quiet:
                self.log("Not connected", "sys")

    def poll(self):
        self.root.after(100, self.poll)
        try:
            while True:
                self.handle(self.q.get_nowait())
        except queue.Empty:
            pass

    def handle(self, m):
        t = m.get("t")
        if t == "up":
            self.main_screen()
            self.send(t="hi", n=self.name.get().strip() or "Friend")
            self.log("Connected. Say hi!", "sys")
        elif t == "hi":
            self.log("%s joined" % m["n"], "sys")
        elif t == "chat":
            self.log("%s: %s" % (m["n"], m["x"]))
        elif t == "ttt":
            self.ttt_move(m["i"], "O" if self.me == "X" else "X")
        elif t == "ttt_reset":
            self.ttt_reset()
        elif t == "rps":
            self.theirs = m["c"]
            self.rps_show()
        elif t == "rps_reset":
            self.rps_reset()
        elif t == "v":
            if self.out and self.play.qsize() < 10:
                self.play.put(base64.b64decode(m["d"]))
        elif t == "bye":
            self.voice_stop()
            self.log("Your friend left", "sys")
        elif t == "err":
            self.status.config(text="Could not connect: " + m["x"])

    # ---------- voice ----------
    def voice_toggle(self):
        if self.inp:
            return self.voice_stop()
        if sd is None:
            self.vlbl.config(text="Voice needs the sounddevice package (pip install sounddevice)")
            return
        try:
            self.out = sd.RawOutputStream(samplerate=RATE, channels=1, dtype="int16",
                                          blocksize=BLOCK, callback=self.speaker)
            self.inp = sd.RawInputStream(samplerate=RATE, channels=1, dtype="int16",
                                         blocksize=BLOCK, callback=self.mic)
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
        self.vbtn.config(text="Start voice")
        self.vlbl.config(text="Voice off")

    def mute_toggle(self):
        self.muted = not self.muted
        self.mbtn.config(text="Unmute mic" if self.muted else "Mute mic")

    def mic(self, data, frames, time, status):
        if not self.muted:
            self.send(quiet=True, t="v", d=base64.b64encode(bytes(data)).decode())

    def speaker(self, outdata, frames, time, status):
        try:
            b = self.play.get_nowait()
        except queue.Empty:
            b = b""
        outdata[:] = b if len(b) == len(outdata) else bytes(len(outdata))

    # ---------- chat ----------
    def log(self, text, tag=""):
        self.chat.config(state="normal")
        self.chat.insert("end", text + "\n", tag)
        self.chat.config(state="disabled")
        self.chat.see("end")

    def say(self, _=None):
        x = self.msg.get().strip()
        if not x:
            return
        self.msg.delete(0, "end")
        n = self.name.get().strip() or "Friend"
        self.log("%s: %s" % (n, x), "me")
        self.send(t="chat", n=n, x=x)

    # ---------- tic-tac-toe ----------
    def ttt_reset(self):
        self.board, self.turn, self.result = [""] * 9, "X", ""
        self.ttt_show()

    def ttt_new(self):
        self.send(t="ttt_reset")
        self.ttt_reset()

    def ttt_click(self, i):
        if self.result or self.board[i] or self.turn != self.me:
            return
        self.ttt_move(i, self.me)
        self.send(t="ttt", i=i)

    def ttt_move(self, i, m):
        b = self.board
        b[i], self.turn = m, "O" if m == "X" else "X"
        w = next((b[x] for x, y, z in LINES if b[x] and b[x] == b[y] == b[z]), None)
        if w:
            self.result = "You win!" if w == self.me else "They win"
        elif all(b):
            self.result = "Draw"
        self.ttt_show()

    def ttt_show(self):
        for b, m in zip(self.cells, self.board):
            b.config(text=m)
        turn = "Your turn (%s)" % self.me if self.turn == self.me else "Waiting for your friend…"
        self.ttt_lbl.config(text=self.result or turn)

    # ---------- rock paper scissors ----------
    def rps_reset(self):
        self.mine = self.theirs = None
        self.rps_show()

    def rps_new(self):
        self.send(t="rps_reset")
        self.rps_reset()

    def rps_pick(self, c):
        if self.mine:
            return
        self.mine = c
        self.send(t="rps", c=c)
        self.rps_show()

    def rps_show(self):
        a, b = self.mine, self.theirs
        if a and b:
            r = "Tie" if a == b else "You win!" if BEATS[a] == b else "They win"
            s = "%s vs %s: %s" % (RPS[a], RPS[b], r)
        elif a:
            s = "You picked %s. Waiting…" % RPS[a]
        else:
            s = "Pick one"
        self.rps_lbl.config(text=s)


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
