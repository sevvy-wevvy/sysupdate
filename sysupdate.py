#!/usr/bin/env python3
import curses
import json
import os
import shutil
import subprocess
import threading
import time

CONF = os.path.expanduser("~/.config/sysupdate")
HOOKS = f"{CONF}/hooks"
NVIDIA = ["nvidia", "nvidia-dkms", "nvidia-open", "nvidia-open-dkms",
          "nvidia-utils", "lib32-nvidia-utils"]
KERNEL = ["linux", "linux-headers", "linux-lts", "linux-lts-headers",
          "linux-zen", "linux-zen-headers"]


def on_ac():
    base = "/sys/class/power_supply"
    for p in os.listdir(base):
        try:
            if open(f"{base}/{p}/online").read().strip() == "1":
                return True
        except OSError:
            pass
    return False


def have(cmd):
    return shutil.which(cmd) is not None


def run(cmd, timeout=90):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL)
        return p.returncode, [l for l in p.stdout.splitlines() if l.strip()]
    except Exception as e:
        return -1, [str(e)]


_cu = []
_cu_lock = threading.Lock()


def checkupdates():
    # checkupdates breaks if 2 run at once
    with _cu_lock:
        if not _cu:
            _cu.append(run(["checkupdates"]))
        return _cu[0]


def count(lines, ok="up to date"):
    return (f"{len(lines)} updates", lines, True) if lines else (ok, [], False)


def check_pacman():
    rc, lines = checkupdates()
    if rc == 2:
        return "up to date", [], False
    if rc != 0:
        return "check failed", lines, None
    return count(lines)


def held(names):
    rc, lines = checkupdates()
    return [l for l in lines if l.split()[0] in names]


def check_nvidia():
    ups = held(NVIDIA)
    return (f"{len(ups)} held back" if ups else "no driver update"), \
        ups + ["When ticked the driver won't update. nvidia-utils gets held too so they match."], None


def check_kernel():
    ups = held(KERNEL)
    return (f"{len(ups)} available" if ups else "no kernel update"), \
        ups + [f"You're on {os.uname().release}. When ticked the kernel won't update.",
               "Okay for a few days, but don't leave it held forever."], None


def check_aur():
    return count(run([aur, "-Qua"])[1])


def check_fwupd():
    run(["fwupdmgr", "refresh"], timeout=60)
    rc, lines = run(["fwupdmgr", "get-updates", "--json"], timeout=60)
    try:
        devs = json.loads("\n".join(lines)).get("Devices", [])
    except ValueError:
        devs = []
    detail = [f"{d.get('Name')}: {d.get('Version')} -> {d['Releases'][0].get('Version')}"
              for d in devs if d.get("Releases")]
    if not on_ac():
        detail.insert(0, "You're on battery, plug in before updating firmware.")
    return (f"{len(devs)} devices", detail, True) if devs else ("up to date", detail, False)


def check_flatpak():
    rc, lines = run(["flatpak", "remote-ls", "--updates", "--columns=application"])
    return count(lines) if rc == 0 else ("check failed", lines, None)


def check_rustup():
    rc, lines = run(["rustup", "check"])
    return count([l for l in lines if "Update available" in l])


def check_npm():
    rc, lines = run(["npm", "outdated", "-g", "--parseable"])
    return count([l.split(":")[-1] for l in lines])


def check_orphans():
    lines = run(["pacman", "-Qdtq"])[1]
    return (f"{len(lines)} orphans", lines, True) if lines else ("none", [], False)


def check_cache():
    lines = run(["paccache", "-dk2"])[1]
    s = next((l for l in lines if "candidate" in l), "?")
    if "no candidate" in s:
        return "nothing to clean", [], False
    return s.replace("==> finished dry run: ", ""), [], True


aur = next((a for a in ("yay", "paru") if have(a)), None)

# key, label, show, check, cmds, default (None = ticked if it has updates)
ITEMS = [
    ("snap", "Snapshot first (snapper)", have("snapper"), None,
     [["sudo", "snapper", "-c", "root", "create", "-c", "number", "-d", "sysupdate"]], True),
    ("mirrors", "Refresh mirrors (reflector)", have("reflector"), None,
     [["sudo", "systemctl", "start", "reflector.service"]], False),
    ("holdnv", "Hold NVIDIA driver", have("nvidia-smi"), check_nvidia, [], False),
    ("holdkernel", "Hold kernel", True, check_kernel, [], False),
    ("pacman", "Arch packages (pacman)", have("checkupdates"), check_pacman,
     [["sudo", "pacman", "-Syu"]], None),
    ("aur", f"AUR packages ({aur})", aur is not None, check_aur,
     [[aur or "yay", "-Sua"]], None),
    ("fwupd", "BIOS & firmware (fwupd)", have("fwupdmgr"), check_fwupd,
     [["fwupdmgr", "update"]], None),
    ("flatpak", "Flatpak apps", have("flatpak"), check_flatpak,
     [["flatpak", "update", "-y"]], None),
    ("rustup", "Rust toolchain (rustup)", have("rustup"), check_rustup,
     [["rustup", "update"]], None),
    ("pipx", "pipx apps", have("pipx"), None, [["pipx", "upgrade-all"]], True),
    ("uv", "uv tools", have("uv"), None, [["uv", "tool", "upgrade", "--all"]], False),
    ("npm", "npm global packages", have("npm"), check_npm,
     [["sudo", "npm", "update", "-g"]], None),
    ("claude", "Claude Code", have("claude"), None, [["claude", "update"]], True),
    ("cache", "Clean package cache (keep 2)", have("paccache"), check_cache,
     [["sudo", "paccache", "-rk2"], ["sudo", "paccache", "-ruk0"]], None),
    ("orphans", "Remove orphan packages", have("pacman"), check_orphans,
     [["sh", "-c", "pacman -Qdtq | sudo pacman -Rns -"]], False),
]

if os.path.isdir(HOOKS):
    for f in sorted(os.listdir(HOOKS)):
        p = os.path.join(HOOKS, f)
        if os.access(p, os.X_OK) and not f.startswith("."):
            ITEMS.append((f"hook:{f}", f, True, None, [[p]], True))

try:
    prefs = dict(l.split()[:2] for l in open(f"{CONF}/defaults") if len(l.split()) >= 2
                 and not l.startswith("#"))
except OSError:
    prefs = {}
ITEMS = [(k, label, show, chk, cmds, prefs[k] == "on" if k in prefs else d)
         for k, label, show, chk, cmds, d in ITEMS if show]
state = {k: {"status": "checking..." if chk else "", "detail": [], "sel": bool(d), "touched": False}
         for k, _, _, chk, _, d in ITEMS}
has_cmds = {k: bool(cmds) for k, _, _, _, cmds, _ in ITEMS}
lock = threading.Lock()


def do_check(key, fn, default):
    s, detail, has = fn()
    with lock:
        st = state[key]
        st["status"], st["detail"] = s, detail
        if not st["touched"] and default is None:
            st["sel"] = bool(has)


def put(scr, *a):
    try:
        scr.addnstr(*a)
    except curses.error:  # last corner, curses always throws there
        pass


def ui(scr):
    curses.curs_set(0)
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_MAGENTA, -1)
    curses.init_pair(2, curses.COLOR_GREEN, -1)
    curses.init_pair(3, curses.COLOR_YELLOW, -1)
    acc, good, warn = (curses.color_pair(i) for i in (1, 2, 3))
    scr.timeout(150)
    pos = 0
    n = len(ITEMS) + 1
    while True:
        scr.erase()
        h, w = scr.getmaxyx()
        put(scr, 0, 2, "System update", w - 3, acc | curses.A_BOLD)
        put(scr, 1, 2, "space/enter toggle · a all · n none · d run · q quit", w - 3, curses.A_DIM)
        with lock:
            for i, (key, label, *_) in enumerate(ITEMS):
                st = state[key]
                y = 3 + i
                if y >= h - 1:
                    break
                box = "[x]" if st["sel"] else "[ ]"
                hi = curses.A_REVERSE if i == pos else 0
                put(scr, y, 2, f" {box} {label:<34}", w - 3, hi | (acc if st["sel"] else 0))
                s = st["status"]
                if s in ("up to date", "none", "nothing to clean", "no driver update", "no kernel update"):
                    col = good
                elif s and s != "checking...":
                    col = warn
                else:
                    col = curses.A_DIM
                put(scr, y, 42, s, max(0, w - 44), col)

            y = 4 + len(ITEMS)
            busy = any(s["status"] == "checking..." for s in state.values())
            picked = sum(s["sel"] for k, s in state.items() if has_cmds[k])
            put(scr, y, 2, f"  [ Done: run {picked} ]  ", w - 3,
                (curses.A_REVERSE if pos == len(ITEMS) else 0) | acc | curses.A_BOLD)
            if busy:
                put(scr, y, 26, " still checking, you can run anyway", w - 28, curses.A_DIM)
            if pos < len(ITEMS):
                for j, line in enumerate(state[ITEMS[pos][0]]["detail"][:max(0, h - y - 3)]):
                    put(scr, y + 2 + j, 4, line, w - 6, curses.A_DIM)
        scr.refresh()

        c = scr.getch()
        if c in (curses.KEY_UP, ord("k")):
            pos = (pos - 1) % n
        elif c in (curses.KEY_DOWN, ord("j"), 9):
            pos = (pos + 1) % n
        elif c in (ord(" "), 10, 13, curses.KEY_ENTER):
            if pos == len(ITEMS):
                return True
            with lock:
                st = state[ITEMS[pos][0]]
                st["sel"], st["touched"] = not st["sel"], True
        elif c in (ord("a"), ord("n")):
            with lock:
                for st in state.values():
                    st["sel"], st["touched"] = c == ord("a"), True
        elif c == ord("d"):
            return True
        elif c in (ord("q"), 27):
            return False


def banner(text):
    print(f"\n\033[1;35m━━ {text} \033[0m", flush=True)


def keep_sudo():
    while True:
        time.sleep(50)
        subprocess.run(["sudo", "-nv"])


def main():
    for key, _, _, chk, _, d in ITEMS:
        if chk:
            threading.Thread(target=do_check, args=(key, chk, d), daemon=True).start()
    try:
        if not curses.wrapper(ui):
            return
    except KeyboardInterrupt:
        return

    todo = [(k, label, cmds) for k, label, _, _, cmds, _ in ITEMS if state[k]["sel"] and cmds]
    skip = (NVIDIA if state.get("holdnv", {}).get("sel") else []) + \
           (KERNEL if state.get("holdkernel", {}).get("sel") else [])
    if skip:
        ign = ["--ignore", ",".join(skip)]
        todo = [(k, label, [c + ign if k in ("pacman", "aur") else c for c in cmds])
                for k, label, cmds in todo]
    if not todo:
        print("Nothing selected.")
        return

    if any("sudo" in " ".join(c) for _, _, cmds in todo for c in cmds):
        banner("sudo")
        if subprocess.run(["sudo", "-v"]).returncode != 0:
            print("sudo failed, stopping.")
            return
        threading.Thread(target=keep_sudo, daemon=True).start()
    if any(k == "fwupd" for k, _, _ in todo) and not on_ac():
        print("\033[33mYou're on battery, plug in before updating firmware.\033[0m")

    results = []
    for key, label, cmds in todo:
        banner(label)
        rc = 0
        for cmd in cmds:
            rc = subprocess.run(cmd).returncode
            if key == "fwupd" and rc == 2:  # 2 = nothing to update
                rc = 0
            if rc:
                break
        results.append((label, rc))
        if key == "snap" and rc:
            if input("\033[33mSnapshot failed, keep going? [y/N] \033[0m").strip().lower() != "y":
                break

    banner("Summary")
    for label, rc in results:
        mark = "\033[32m✓\033[0m" if rc == 0 else f"\033[31m✗ (exit {rc})\033[0m"
        print(f"  {mark} {label}")
    if not os.path.isdir(f"/usr/lib/modules/{os.uname().release}"):
        print("\n\033[1;33mKernel got updated, reboot when you can.\033[0m")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted.")
    if os.environ.get("SYSUPDATE_WAIT"):
        input("\nPress Enter to close.")
