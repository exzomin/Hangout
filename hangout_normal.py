#!/usr/bin/env python3
"""Hangout: chat, voice, screen sharing and games for up to 5 people.
One person clicks Host, the others type the host's address and click Join."""
import array, base64, io, json, queue, socket, threading, time, tkinter as tk
from tkinter import messagebox

try:
    import sounddevice as sd  # voice chat
except Exception:
    sd = None
try:
    from PIL import Image, ImageGrab, ImageTk  # screen sharing
except Exception:
    Image = None

PORT, MAX = 5000, 5
RATE, BLOCK = 16000, 640  # 16 kHz mono, 40 ms per voice packet
BG, BG2, FG, ACC = "#313338", "#2b2d31", "#f2f3f5", "#5865f2"
LINES = [(0, 1, 2), (3, 4, 5), (6, 7, 8), (0, 3, 6), (1, 4, 7), (2, 5, 8), (0, 4, 8), (2, 4, 6)]
RPS = {"r": "Rock", "p": "Paper", "s": "Scissors"}
BEATS = {"r": "s", "s": "p", "p": "r"}
MARK = {0: "X", 1: "O"}  # host plays X, the first joiner plays O, others watch


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


def head(p, text):
    return tk.Label(p, text=text, font=("Arial", 12, "bold"), bg=BG2, fg=FG)


class App:
    def __init__(self, root):
        self.root, self.q, self.lock = root, queue.Queue(), threading.Lock()
        self.name, self.ip = tk.StringVar(value="Me"), tk.StringVar()
        self.hub, self.me, self.sock, self.conns, self.names = False, 0, None, {}, {}
        self.inp = self.out = None
        self.muted = self.sharing = False
        self.vq, self.view = {}, {}
        self.board, self.turn, self.res, self.picks = [""] * 9, "X", "", {}
        root.title("Hangout")
        root.configure(bg=BG)
        self.connect_screen()
        root.after(20, self.poll)

    def nm(self):
        return self.name.get().strip() or "Friend"

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
        btn(f, "Host (up to 4 friends join you)", self.host).pack(pady=(14, 4))
        tk.Label(f, text="or join a friend's address", bg=BG, fg=FG).pack(pady=(10, 0))
        tk.Entry(f, textvariable=self.ip, justify="center").pack(pady=4)
        btn(f, "Join", self.join).pack()
        self.status = tk.Label(f, text="", bg=BG, fg=FG, wraplength=260)
        self.status.pack(pady=10)

    def main_screen(self):
        self.clear()
        self.root.geometry("820x640")
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

        r = tk.Frame(self.root, bg=BG2, padx=14, pady=10)
        r.grid(row=0, column=1, sticky="ns")
        
        # Odadan Çıkma Butonu
        btn(r, "Odadan Çık", self.leave_room, bg="#ed4245").pack(fill="x", pady=(0, 8))

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
            b = btn(grid, "", lambda i=i: self.act({"t": "ttt_move", "i": i}), bg=BG, font=("Arial", 18), width=3)
            b.grid(row=i // 3, column=i % 3, padx=2, pady=2)
            self.cells.append(b)
        self.ttt_lbl = tk.Label(r, bg=BG2, fg=FG)
        self.ttt_lbl.pack()
        btn(r, "New game", lambda: self.act({"t": "ttt_reset"})).pack(pady=4)
        head(r, "Rock Paper Scissors").pack(pady=(10, 4))
        row = tk.Frame(r, bg=BG2)
        row.pack()
        for k, v in RPS.items():
            btn(row, v, lambda k=k: self.act({"t": "rps_pick", "c": k})).pack(side="left", padx=2)
        self.rps_lbl = tk.Label(r, bg=BG2, fg=FG, wraplength=230)
        self.rps_lbl.pack(pady=4)
        btn(r, "New round", lambda: self.act({"t": "rps_reset"})).pack()

    # ---------- leave room ----------
    def leave_room(self):
        self.voice_stop()
        self.sharing = False
        if self.hub:
            for c in list(self.conns.values()):
                try:
                    c.close()
                except Exception:
                    pass
            self.conns.clear()
            self.hub = False
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None
        for f, w in list(self.view.items()):
            if isinstance(w, tuple):
                try:
                    w[0].destroy()
                except Exception:
                    pass
        self.view.clear()
        self.names.clear()
        self.board = [""] * 9
        self.res = ""
        self.picks = {}
        self.connect_screen()

    # ---------- networking ----------
    def host(self):
        self.hub, self.me = True, 0
        threading.Thread(target=self.serve, daemon=True).start()
        self.main_screen()
        self.log("You are hosting. Give up to 4 friends this address: %s" % local_ip(), "sys")
        self.act({"t": "hi", "n": self.nm()})

    def serve(self):
        try:
            s = socket.socket()
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("", PORT))
            s.listen(4)
        except OSError as e:
            self.q.put((None, {"t": "err", "x": str(e)}))
            return
        while True:
            c, _ = s.accept()
            k = next((i for i in range(1, MAX) if i not in self.conns), None)
            if k is None:
                c.close()
                continue
            self.conns[k] = c
            self.raw(c, {"t": "id", "i": k})
            threading.Thread(target=self.read, args=(c, k), daemon=True).start()

    def join(self):
        addr = self.ip.get().strip()
        if not addr:
            self.status.config(text="Type the host's address first.")
            return
        self.status.config(text="Connecting…")

        def run():
            try:
                c = socket.create_connection((addr, PORT), timeout=8)
                c.settimeout(None)
            except OSError as e:
                self.q.put((None, {"t": "err", "x": str(e)}))
                return
            self.sock = c
            self.q.put((None, {"t": "up"}))
            self.read(c, None)

        threading.Thread(target=run, daemon=True).start()

    def read(self, c, k):
        try:
            for line in c.makefile("r", encoding="utf-8"):
                self.q.put((k, json.loads(line)))
        except (OSError, ValueError):
            pass
        self.q.put((k, {"t": "bye"}))

    def raw(self, c, m):
        try:
            with self.lock:
                c.sendall((json.dumps(m) + "\n").encode())
        except OSError:
            pass

    def bcast(self, m, skip=None, local=True):
        for k, c in list(self.conns.items()):
            if k != skip:
                self.raw(c, m)
        if local:
            self.show(m)

    def act(self, m):  # something this person did (main thread)
        if self.hub:
            m["f"] = 0
            self.on_msg(m)
        elif self.sock:
            self.raw(self.sock, m)

    def media(self, m):  # voice or screen data (any thread)
        m["f"] = self.me
        if self.hub:
            self.bcast(m, skip=self.me, local=False)
        elif self.sock:
            self.raw(self.sock, m)

    def poll(self):
        self.root.after(20, self.poll)
        messages = []
        while True:
            try:
                k, m = self.q.get_nowait()
                messages.append((k, m))
            except queue.Empty:
                break
        
        latest_scr = {}
        others = []
        for k, m in messages:
            if m.get("t") == "scr":
                latest_scr[m.get("f")] = (k, m)
            else:
                others.append((k, m))
                
        for k, m in others:
            try:
                self.inbox(k, m)
            except (KeyError, ValueError, TypeError, IndexError):
                pass
                
        for f, (k, m) in latest_scr.items():
            try:
                self.inbox(k, m)
            except (KeyError, ValueError, TypeError, IndexError):
                pass

    def inbox(self, k, m):
        t = m.get("t")
        if t == "err":
            messagebox.showerror("Hangout", "Could not connect: " + m["x"])
            self.hub = False
            self.connect_screen()
        elif self.hub:
            if t == "bye":
                c = self.conns.pop(k, None)
                if c:
                    c.close()
                self.picks.pop(k, None)
                self.bcast(self.names_msg())
            else:
                m["f"] = k
                self.on_msg(m)
        elif t == "up":
            self.main_screen()
            self.act({"t": "hi", "n": self.nm()})
        elif t == "id":
            self.me = m["i"]
        elif t == "bye":
            self.voice_stop()
            self.sharing = False
            self.sock = None
            self.log("The host left", "sys")
        else:
            self.show(m)

    # ---------- host logic (the host keeps the shared state) ----------
    def names_msg(self):
        return {"t": "names", "d": {str(k): v for k, v in self.names.items() if k == 0 or k in self.conns}}

    def ttt_msg(self):
        return {"t": "ttt_state", "b": list(self.board), "turn": self.turn, "w": self.res}

    def rps_msg(self):
        ids = [0] + list(self.conns)
        done = len(ids) > 1 and all(i in self.picks for i in ids)
        return {"t": "rps_state", "p": list(self.picks), "r": {str(k): c for k, c in self.picks.items()} if done else None}

    def on_msg(self, m):
        t, f, b = m["t"], m["f"], self.board
        if t == "hi":
            self.names[f] = m["n"]
            self.bcast(self.names_msg())
            self.bcast(self.ttt_msg())
            self.bcast(self.rps_msg())
        elif t == "ttt_move":
            i = m["i"]
            if MARK.get(f) == self.turn and not self.res and not b[i]:
                b[i], self.turn = self.turn, "O" if self.turn == "X" else "X"
                w = next((b[x] for x, y, z in LINES if b[x] and b[x] == b[y] == b[z]), "")
                self.res = w or ("D" if all(b) else "")
                self.bcast(self.ttt_msg())
        elif t == "ttt_reset":
            self.board, self.turn, self.res = [""] * 9, "X", ""
            self.bcast(self.ttt_msg())
        elif t == "rps_pick":
            self.picks.setdefault(f, m["c"])
            self.bcast(self.rps_msg())
        elif t == "rps_reset":
            self.picks = {}
            self.bcast(self.rps_msg())
        else:  # chat, voice, screen: pass on to everyone else
            self.bcast(m, skip=f, local=f != 0)

    # ---------- showing what arrives ----------
    def show(self, m):
        t = m["t"]
        if t == "names":
            new = {int(k): v for k, v in m["d"].items()}
            if self.names:
                for k, v in new.items():
                    if k not in self.names and k != self.me:
                        self.log("%s joined" % v, "sys")
                for k, v in self.names.items():
                    if k not in new:
                        self.log("%s left" % v, "sys")
                        self.vq.pop(k, None)
                        self.close_view(k)
            self.names = new
            self.ppl.config(text="In the room (%d/%d): %s" % (len(new), MAX, ", ".join(new.values())))
        elif t == "chat":
            self.log("%s: %s" % (self.names.get(m["f"], "?"), m["x"]))
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
            mk = MARK.get(self.me, "")
            for c, v in zip(self.cells, self.board):
                c.config(text=v)
            if self.res:
                s = "Draw" if self.res == "D" else "You win!" if self.res == mk else "%s wins" % self.res
            elif not mk:
                s = "Watching"
            else:
                s = "Your turn (%s)" % mk if self.turn == mk else "Waiting…"
            self.ttt_lbl.config(text=s)
        elif t == "rps_state":
            r = m["r"]
            if r:
                nm = lambda k: self.names.get(int(k), "?")
                s = set(r.values())
                if len(s) == 2:
                    a, b = s
                    w = a if BEATS[a] == b else b
                    res = "Winner: " + ", ".join(nm(k) for k, c in r.items() if c == w)
                else:
                    res = "Tie"
                txt = ", ".join("%s: %s" % (nm(k), RPS[c]) for k, c in r.items()) + "\n" + res
            else:
                txt = "Picked %d of %d. %s" % (len(m["p"]), len(self.names), "Waiting…" if self.me in m["p"] else "Pick one")
            self.rps_lbl.config(text=txt)

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
        self.log("%s: %s" % (self.nm(), x), "me")
        self.act({"t": "chat", "x": x})

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

    def mic(self, data, frames, time, status):
        if not self.muted:
            self.media({"t": "v", "d": base64.b64encode(bytes(data)).decode()})

    def speaker(self, outdata, frames, time, status):  # mixes everyone who is talking
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
                self.media({"t": "scr", "d": base64.b64encode(buf.getvalue()).decode()})
            except Exception:
                self.sharing = False
                break
            elapsed = time.time() - start_time
            if elapsed < interval:
                time.sleep(interval - elapsed)
        self.media({"t": "scr_stop"})

    def show_screen(self, f, data):
        w = self.view.get(f)
        if Image is None or w == "x":
            return
        img = ImageTk.PhotoImage(Image.open(io.BytesIO(base64.b64decode(data))))
        if not w:
            top = tk.Toplevel(self.root)
            top.title("Screen: " + self.names.get(f, "?"))
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