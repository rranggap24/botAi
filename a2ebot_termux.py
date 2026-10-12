"""
a2ebot_termux.py - Lightweight Termux/Android-compatible clone of a2ebot.py

Differences from a2ebot.py (Windows desktop):
  1. No warp-cli dependency          -> proxy via env PROXY= OR --proxy= arg
  2. Headless Chrome by default     -> no Xvfm needed
  3. No taskkill / Windows paths    -> relies on SB context-manager cleanup
  4. Credentials sourced from JSON  -> avoids hardcoding in script
  5. UTF-8 console forced           -> Termux already UTF-8

Requirements (Termux packages):
  pkg install python chromium clang fftw freetype harfbuzz fribidi \
              libpng jpeg-turbo openjpeg zlib
  pip install seleniumbase undetected-chromedriver beautifulsoup4

Usage:
  python a2ebot_termux.py                       # headless Chrome
  python a2ebot_termux.py --proxy http://...   # set proxy
  python a2ebot_termux.py --accounts akun.json  # custom file
"""
import os
import sys
import time
import json
import random
import urllib.request
import traceback
import threading
import gc
import re

# --- ANSI colours (mirrors a2ebot.py palette; DIM already fixed) ---
ESC = chr(27)
R = ESC + "[91m"    # Merah
G = ESC + "[92m"    # Hijau
Y = ESC + "[93m"    # Kuning
B = ESC + "[94m"    # Biru
C = ESC + "[96m"    # Cyan
Wcol = ESC + "[97m" # Putih
RST = ESC + "[0m"   # Reset
BLD = ESC + "[1m"   # Bold
DIM = ESC + "[2m"   # Dim / faint
BARIS = chr(0x2501) * 62

# Force UTF-8 stdout
try:
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def log(emoji, color, msg):
    print(f"{BLD}{color}{emoji}  {msg}{RST}", flush=True)


def section(title, emoji=chr(0x1F4CB)):
    width = 62
    line = chr(0x2501) * width
    print(f"\n{BLD}{C}{line}{RST}")
    print(f"{BLD}{C} {emoji}  {title}{RST}")
    print(f"{B}{line}{RST}")


def result_box(sukses, gagal, total):
    bar_eq = "=" * 56
    bar_dash = "-" * 56
    print(f"\n{G}{bar_eq}{RST}")
    print(f"{BLD}{G}  {chr(0x1F4CA)}  RINGKASAN AKHIR - ANTREMU SELESAI{RST}")
    print(f"{G}{bar_dash}{RST}")
    print(f"  {Wcol}Akun Diproses     {C}:{RST} {total}")
    print(f"  {Wcol}Berhasil          {G}:{BLD}{G} {sukses}{RST}")
    print(f"  {Wcol}Gagal             {R}:{BLD}{R} {gagal}{RST}")
    print(f"{G}{bar_eq}{RST}\n")


# --- Graceful crash handler ---
def handle_exception(exc_type, exc_value, exc_traceback):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    print(f"\n[ UNCAUGHT EXCEPTION ] {exc_type.__name__}: {exc_value}", flush=True)
    traceback.print_exception(exc_type, exc_value, exc_traceback, file=sys.stdout, limit=8)
    sys.stdout.flush()


sys.excepthook = handle_exception


def _thread_hook(args):
    print(f"\n[ THREAD CRASH [{args.thread.name}] ] {args.exc_type.__name__}: {args.exc_value}", flush=True)
    traceback.print_exception(args.exc_type, args.exc_value, args.exc_traceback, file=sys.stdout, limit=8)
    sys.stdout.flush()


threading.excepthook = _thread_hook


# --- Helper network functions (IP check, proxy-aware) ---
def cek_ip_publik_saat_ini(proxy=None):
    """Mengambil alamat IP publik melalui proxy (jika disediakan)."""
    try:
        url_target = "https://api.ipify.org?format=json"
        req = urllib.request.Request(url_target, headers={"User-Agent": "Mozilla/5.0"})
        opener = urllib.request.build_opener()
        if proxy:
            proxy_handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
            opener = urllib.request.build_opener(proxy_handler)
        with opener.open(req, timeout=5) as response:
            data_json = json.loads(response.read().decode("utf-8"))
            return data_json["ip"].strip()
    except Exception:
        try:
            with opener.open("https://ident.me", timeout=4) as response:
                return response.read().decode("utf-8").strip()
        except Exception:
            try:
                with opener.open("https://www.cloudflare.com/cdn-cgi/trace", timeout=4) as response:
                    data = response.read().decode("utf-8")
                    for line in data.splitlines():
                        if line.startswith("ip="):
                            return line.split("=", 1)[1].strip()
            except Exception:
                pass
            return "GAGAL_BACA_IP"


def cek_ip_baru(ip_awal, proxy=None):
    """Verifikasi apakah alamat IP publik sudah berubah."""
    ip_baru = cek_ip_publik_saat_ini(proxy)
    berubah = ip_baru != "GAGAL_BACA_IP" and ip_baru != ip_awal
    return ip_baru, berubah


# =========================================================================
# SELENIUM HELPER - inline minimal version of _selhelper.py
# Translates :has-text(...) / :contains(...) pseudo-classes to XPath.
# =========================================================================
def _xquote(t):
    """XPath string literal that survives single/double quotes."""
    if "'" not in t:
        return "'" + t + "'"
    if '"' not in t:
        return '"' + t + '"'
    return "concat(" + ", ".join("'" + x + "'" for x in t.split("'")) + ")"


def _split_tag_and_rest(pre):
    pre = pre.strip()
    if not pre or pre == "*":
        return "*", ""
    tm = re.match(r'^(?:\*[\w:-]*)', pre)
    if tm:
        return tm.group(1), pre[tm.end():]
    return "*", pre


def _piece_css_to_xpath(piece):
    pieces = piece.split()
    out = []
    for sub in pieces:
        tag, rest = _split_tag_and_rest(sub)
        conds = []
        for cm in re.finditer(r'\.([\w-]+)', rest):
            conds.append("contains(@class, '" + cm.group(1) + "')")
        for am in re.finditer(r'([a-zA-Z-]+)([*^]?=)\s*[\'"]([^\'"]*)[\'"]', rest):
            attr, op, val = am.group(1), am.group(2), am.group(3)
            if op == "*=":
                conds.append("contains(@" + attr + ", " + _xquote(val) + ")")
            else:
                conds.append("@" + attr + "=" + _xquote(val))
        if conds:
            out.append(tag + "[" + " and ".join(conds) + "]")
        else:
            out.append(tag)
    return "//" + "//".join(out)


def _piece_to_xpath(piece):
    piece = piece.strip()
    if not piece:
        return ""
    m = re.search(r":(?:has-text|contains)\('([^']*)'\)", piece)
    if m:
        text = m.group(1)
        pre = piece[:m.start()].strip()
        tag, rest = _split_tag_and_rest(pre)
        low = text.lower()
        conds = ["contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), " + _xquote(low) + ")"]
        for cm in re.finditer(r'\.([\w-]+)', rest):
            conds.append("contains(@class, '" + cm.group(1) + "')")
        cond = " and ".join(conds)
        if tag and tag != "*":
            return "//" + tag + "[" + cond + "]"
        return "//*[" + cond + "]"
    return _piece_css_to_xpath(piece)


def to_xpath(expr):
    """Return XPath for selectors that use :has-text/:contains, else None."""
    if not isinstance(expr, str):
        return None
    s = expr.strip()
    if not s:
        return None
    if ":has-text" not in s and ":contains" not in s:
        return None
    parts = [p.strip() for p in s.split(",") if p.strip()]
    xps = [_piece_to_xpath(p) for p in parts]
    xps = [x for x in xps if x]
    if not xps:
        return None
    return "|".join(xps)


def sbi(sb, selector, *args, **kwargs):
    """is_element_visible wrapper: auto-translate :has-text/:contains -> xpath."""
    if isinstance(selector, str):
        xp = to_xpath(selector)
        if xp:
            kwargs["by"] = "xpath"
            selector = xp
    return sb.is_element_visible(selector, *args, **kwargs)


def _try_js_click(sb, sel, by):
    """JS click fallback (scrollIntoView + native MouseEvent dispatch)."""
    try:
        el = sb.find_element(sel, by=by)
        sb.execute_script(
            "var e=arguments[0];e.scrollIntoView({block:'center',inline:'center'});"
            "e.dispatchEvent(new MouseEvent('click',{view:window,bubbles:true,cancelable:true,clientX:1,clientY:1}));",
            el,
        )
        return el
    except Exception:
        return None


def sbc(sb, selector, *args, **kwargs):
    """click wrapper: auto-translate :has-text/:contains -> xpath + JS fallback."""
    by = kwargs.get("by", "css selector")
    if isinstance(selector, str):
        xp = to_xpath(selector)
        if xp:
            by = "xpath"
            selector = xp
    try:
        return sb.click(selector, *args, **{**kwargs, "by": by})
    except Exception:
        if _try_js_click(sb, selector, by) is None:
            try:
                sb.execute_script(
                    "var btns=document.querySelectorAll('button');"
                    "for(var i=0;i<btns.length;i++){"
                    "var t=(btns[i].textContent||'').trim().toLowerCase();"
                    "if(t.indexOf('sign in')!==-1){btns[i].scrollIntoView({block:'center'});"
                    "btns[i].dispatchEvent(new MouseEvent('click',{view:window,bubbles:true,cancelable:true}));"
                    "return btns[i].id||'clicked';}}return 'not-found';"
                )
            except Exception:
                pass
        raise


def sbuc(sb, selector, *args, **kwargs):
    """uc_click wrapper: auto-translate :has-text/:contains -> xpath + JS fallback."""
    by = kwargs.get("by", "css selector")
    if isinstance(selector, str):
        xp = to_xpath(selector)
        if xp:
            by = "xpath"
            selector = xp
    try:
        return sb.uc_click(selector, *args, **{**kwargs, "by": by})
    except Exception:
        el = _try_js_click(sb, selector, by)
        if el is None:
            raise
        return el


def sbg(sb, selector, *args, **kwargs):
    """get_text wrapper: auto-translate :has-text/:contains -> xpath."""
    if isinstance(selector, str):
        xp = to_xpath(selector)
        if xp:
            kwargs["by"] = "xpath"
            selector = xp
        return sb.get_text(selector, *args, **kwargs)


# =========================================================================
# TURNSTILE SOLVER - mirrors klik_turnstile() from a2ebot.py
# =========================================================================
def klik_turnstile(sb):
    """Attempt to solve Cloudflare Turnstile/CAPTCHA with escalating strategies."""

    def _challenge_masih_ada():
        try:
            widget = sb.execute_script(
                "return !!document.querySelector('div.cf-turnstile')"
                "|| !!document.querySelector('iframe[src*=turnstile]')"
                "|| !!document.querySelector('iframe[src*=challenges.cloudflare]');"
            )
            if not widget:
                token = sb.execute_script(
                    "var t=document.querySelector('input[name=\"cf-turnstile-response\"]');return t?(t.value||'empty'):'no-input';"
                )
                if token in ("empty", "no-input"):
                    return False
            return True
        except Exception:
            return False

    # 1. OS-level GUI click
    try:
        sb.uc_gui_click_captcha()
        sb.sleep(3)
        if not _challenge_masih_ada():
            log(chr(0x2705), G, "Turnstile via uc_gui_click_captcha().")
            return True
    except Exception:
        log(chr(0x26A0), Y, "uc_gui_click_captcha gagal, coba strategi lain...")

    # 2. UC click on Turnstile iframe/div
    for sel in ["iframe[src*='turnstile']", "div.cf-turnstile", "div.cf-turnstile iframe"]:
        try:
            if sbi(sb, sel):
                sbuc(sb, sel, timeout=10)
                sb.sleep(3)
                if not _challenge_masih_ada():
                    log(chr(0x2705), G, "Turnstile via uc_click('" + sel + "').")
                    return True
        except Exception:
            pass

    # 3. uc_gui_handle_captcha (if available)
    try:
        if hasattr(sb, "uc_gui_handle_captcha"):
            sb.uc_gui_handle_captcha()
            sb.sleep(3)
            if not _challenge_masih_ada():
                log(chr(0x2705), G, "Turnstile via uc_gui_handle_captcha().")
                return True
    except Exception:
        log(chr(0x26A0), Y, "uc_gui_handle_captcha gagal, coba strategi lain...")

    # 4. Keyboard Tab + Space fallback
    try:
        for _ in range(5):
            sb.uc_gui_press_key("\t")
            sb.sleep(0.2)
        sb.uc_gui_press_key(" ")
        sb.sleep(3)
        if not _challenge_masih_ada():
            log(chr(0x2705), G, "Turnstile via keyboard Tab+Space.")
            return True
    except Exception:
        log(chr(0x26A0), Y, "Keyboard Tab+Space gagal.")

        log(chr(0x274C), R, "Semua strategi Turnstile gagal.")
    return False


def cek_ip_limited(sb):
    """Deteksi apakah halaman menampilkan pesan IP limit / rate limit."""
    error_selectors = [
        "div.aae-ant-message-error", "div.ant-message-error",
        ".aae-ant-message-custom-content", ".ant-message-custom-content",
        ".ant-alert-error", "div[class*='message-error']",
        "div.aae-ant-alert-error",
    ]
    for sel in error_selectors:
        try:
            if sbi(sb, sel):
                error_text = sbg(sb, sel)
                lower = error_text.lower()
                if any(kw in lower for kw in [
                    "too many accounts", "from this ip", "ip has been",
                    "rate limit", "too many", "flagged", "blocked",
                    "try again", "denied", "access denied", "your ip", "ip limit",
                ]):
                    return True, error_text
        except Exception:
            pass
    return False, ""


def lakukan_sign_out(sb, username):
    """Logout dan bersihkan modal."""
    log(chr(0x1F6AA), C, "Memulai proses Logout untuk " + username + "...")
    try:
        if sbi(sb, ".aae-ant-modal-wrap"):
            for sel_close in ["span[aria-label='close']", "button.aae-ant-modal-close", ".aae-ant-modal-wrap"]:
                if sbi(sb, sel_close):
                    sbc(sb, sel_close)
                    sb.sleep(2)
                    break

        selector_avatar = "img[alt='avatar'], div[class*='userProfile'], span.userName___n36Vr"
        if sbi(sb, selector_avatar):
            try:
                sbc(sb, selector_avatar)
            except Exception:
                sb.execute_script(
                    "var imgs=document.querySelectorAll('img[alt=\"avatar\"]');if(imgs.length)"
                    "imgs[0].click();else{var m=document.querySelector('div[class*=userProfile]');"
                    "if(m)m.click();}"
                )
            sb.sleep(2)

        selector_logout = "span:contains('Sign out'), li:contains('Log out'), span:contains('Log out')"
        if sbi(sb, selector_logout):
            sbc(sb, selector_logout)
            log(chr(0x2705), G, "Sesi akun berhasil ditutup dengan aman.")
            sb.sleep(5)
            sb.wait_for_ready_state_complete()
    except Exception:
        pass


def handle_turnstile_ip_limit(sb, cred, ip_awal, proxy):
    """If Turnstile fails, check IP limit and flag for proxy retry."""
    ip_limited, ip_limit_text = cek_ip_limited(sb)
    if ip_limited or sb.is_element_present("iframe[src*='turnstile']") or sb.is_element_present("div.cf-turnstile"):
        log(chr(0x1F6AB), R, "Turnstile gagal + IP limit. Ganti proxy & retry.")
        lakukan_sign_out(sb, cred["username"])
        ip_baru, berubah = cek_ip_baru(ip_awal, proxy)
        if berubah:
            log(chr(0x1F30D), G, "Proxy berhasil diganti! IP baru: " + ip_baru)
        return True
    return False


# =========================================================================
# MAIN PROCESS - login -> Turnstile -> bonus claim
# =========================================================================
def proses_akun(cred, proxy=None, headless=True):
    """Full login flow per account: open a2e.ai -> login -> Turnstile -> bonus."""
    username = cred.get("username", "")
    password = cred.get("password", "")

    if not username or not password:
        log(chr(0x26A0), Y, "Akun tidak memiliki username/password - dilewati.")
        return {"status": "fail", "reason": "missing credentials"}

    ip_awal = cek_ip_publik_saat_ini(proxy)
    proxy_info = ("proxy " + proxy) if proxy else "tanpa proxy"
    log(chr(0x1F30D), B, f"IP saat ini ({proxy_info}): {ip_awal}")

    url = "https://a2e.ai"
    sb = None
    try:
        from seleniumbase import SB

        with SB(uc=True, headless=headless, proxy=proxy,
                disable_gpu=True, no_sandbox=True,
                agent="Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
                window_size="1280,800") as sb:

            log(chr(0x1F50D), C, f"Membuka {url} untuk akun {username}...")
            sb.open(url)
            sb.wait_for_ready_state_complete()

            # --- click login button ---
            try:
                login_sel = "button:has-text('Sign In'), a:has-text('Sign In'), button:has-text('Login'), a:has-text('Login')"
                if sbi(sb, login_sel):
                    sbc(sb, login_sel)
                else:
                    sb.execute_script(
                        "var btns=document.querySelectorAll('button,a');"
                        "for(var i=0;i<btns.length;i++){"
                        "var t=(btns[i].textContent||'').trim().toLowerCase();"
                        "if(t.indexOf('sign in')!==-1||t.indexOf('login')!==-1){"
                        "btns[i].scrollIntoView({block:'center'});"
                        "btns[i].dispatchEvent(new MouseEvent('click',{view:window,bubbles:true,cancelable:true}));"
                        "return;}}}"
                    )
            except Exception:
                log(chr(0x26A0), Y, "Gagal klik login via normal - mencoba JS fallback.")
                try:
                    sb.execute_script(
                        "var btn=document.querySelector('button[data-testid=\"login\"], a[href*=\"login\"]');if(btn)btn.click();"
                    )
                except Exception:
                    pass

            sb.sleep(3)
            sb.wait_for_ready_state_complete()

            # --- Turnstile on login page ---
            if not klik_turnstile(sb):
                retry = handle_turnstile_ip_limit(sb, cred, ip_awal, proxy)
                if retry:
                    return {"status": "retry", "reason": "turnstile+iplimit", "ip_awal": ip_awal}
                log(chr(0x274C), R, f"Gagal login {username} - Turnstile tidak bisa dipecahkan.")
                return {"status": "fail", "reason": "turnstile_failed", "username": username}

            # --- inject credentials via JS ---
            log(chr(0x1F511), B, "Menginject kredensial via JS...")
            repr_u = repr(username)
            repr_p = repr(password)
            jsn = (
                "var u=" + repr_u + ";var p=" + repr_p + ";\n"
                "var inp=document.querySelector('input[name=\"email\"],input[name=\"username\"],input[type=\"email\"]');"
                "if(inp){inp.value=u;inp.dispatchEvent(new Event('input',{bubbles:true}));}\n"
                "var pw=document.querySelector('input[name=\"password\"],input[type=\"password\"]');"
                "if(pw){pw.value=p;pw.dispatchEvent(new Event('input',{bubbles:true}));}\n"
                "var sub=document.querySelector('button[type=\"submit\"]');"
                "if(sub)sub.click();else{var f=document.querySelector('form');if(f)f.submit();}"
            )
            sb.execute_script(jsn)

            sb.sleep(3)
            sb.wait_for_ready_state_complete()

            # --- second Turnstile after login attempt ---
            if not klik_turnstile(sb):
                retry = handle_turnstile_ip_limit(sb, cred, ip_awal, proxy)
                if retry:
                    return {"status": "retry", "reason": "turnstile+iplimit", "ip_awal": ip_awal}

            # --- verify login succeeded ---
            login_ok = False
            for sel in ["div[class*='profile']", "span.userName___n36Vr", "img[alt='avatar']", ".ant-dropdown"]:
                if sbi(sb, sel):
                    login_ok = True
                    break
            if not login_ok:
                try:
                    current_url = sb.get_current_url()
                    if "dashboard" in current_url or "profile" in current_url:
                        login_ok = True
                except Exception:
                    pass
            if not login_ok:
                log(chr(0x274C), R, f"Login gagal untuk akun {username}.")
                return {"status": "fail", "reason": "login_failed", "username": username}

            log(chr(0x2728), G, f"Login berhasil untuk akun {username}!")

            # --- claim daily bonus ---
            claimed = False
            for txt in ["Claim", "Get Reward", "Check in"]:
                for tag in ["button", "a"]:
                    sel = tag + ":has-text('" + txt + "')"
                    try:
                        if sbi(sb, sel):
                            sbc(sb, sel)
                            log(chr(0x1F4B0), G, f"Bonus harian diklaim: " + txt + "!")
                            claimed = True
                            sb.sleep(3)
                            break
                    except Exception:
                        pass
                if claimed:
                    break

            if not claimed:
                try:
                    sb.execute_script(
                        "var btns=document.querySelectorAll('button,a');"
                        "for(var i=0;i<btns.length;i++){"
                        "var t=(btns[i].textContent||'').trim().toLowerCase();"
                        "if(t.indexOf('claim')!==-1||t.indexOf('get reward')!==-1"
                        "||t.indexOf('check in')!==-1){"
                        "btns[i].scrollIntoView({block:'center'});"
                        "btns[i].dispatchEvent(new MouseEvent('click',{view:window,bubbles:true,cancelable:true}));"
                        "}}}}"
                    )
                    sb.sleep(3)
                    log(chr(0x1F4B0), G, "Bonus harian diklaim (JS fallback)!")
                except Exception:
                    log(chr(0x274C), R, "Gagal mengklik tombol bonus sama sekali.")

            # --- post-claim Turnstile handling ---
            if not klik_turnstile(sb):
                ip_limited, ip_text = cek_ip_limited(sb)
                if ip_limited:
                    log(chr(0x1F6AB), R, "IP limited setelah klaim - ganti proxy & retry.")
                    lakukan_sign_out(sb, username)
                    ip_baru, berubah = cek_ip_baru(ip_awal, proxy)
                    if berubah:
                        log(chr(0x1F30D), G, "Proxy diganti! IP baru: " + ip_baru)
                    return {"status": "retry", "reason": "iplimit_post_claim", "ip_awal": ip_awal}

            log(chr(0x2705), G, f"Akun {username} selesai - bonus klaim selesai.")
            return {"status": "success", "username": username, "ip_awal": ip_awal}

    except Exception as e:
        log(chr(0x274C), R, f"Error memproses akun {username}: {str(e)[:200]}")
        traceback.print_exc()
        return {"status": "fail", "reason": str(e)[:200], "username": username}
    finally:
        if sb is not None:
            try:
                sb.tearDown()
                gc.collect()
            except Exception:
                pass


# ========================================================================
# CREDENTIAL LOADING
# ========================================================================
def load_credentials(filepath=None):
    """Load accounts from JSON. Expected: [{"username":..., "password":...}]"""
    if filepath is None:
        filepath = os.path.join(os.path.expanduser("~"), "a2e_accounts.json")
    if not os.path.isfile(filepath):
        log(chr(0x26A0), Y, "File akun tidak ditemukan: " + str(filepath))
        return []
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            valid = [c for c in data if isinstance(c, dict) and c.get("username") and c.get("password")]
            if len(valid) < len(data):
                log(chr(0x26A0), Y, str(len(data) - len(valid)) + " akun tidak valid - diabaikan.")
            return valid
        elif isinstance(data, dict) and "accounts" in data:
            accounts = data["accounts"]
            valid = [c for c in accounts if isinstance(c, dict) and c.get("username") and c.get("password")]
            return valid
        else:
            log(chr(0x26A0), Y, "Format JSON tidak dikenal.")
            return []
    except Exception as e:
        log(chr(0x274C), R, "Gagal load akun dari " + str(filepath) + ": " + str(e)[:200])
        return []


# ========================================================================
# CLI ARGUMENT PARSING
# ========================================================================
def parse_args():
    """Parse command-line arguments."""
    import argparse
    parser = argparse.ArgumentParser(description="a2ebot_termux.py - Airdrop2Earn auto bot")
    parser.add_argument("--proxy", type=str, default=None, help="Proxy URL (e.g. http://host:port)")
    parser.add_argument("--accounts", type=str, default=None, help="Path to JSON accounts file")
    parser.add_argument("--no-headless", action="store_true", default=False, help="Run Chrome with GUI")
    parser.add_argument("--max-retries", type=int, default=3, help="Max retries per account (default: 3)")
    parser.add_argument("--cooldown-min", type=int, default=10, help="Min cooldown between accounts (default: 10)")
    parser.add_argument("--cooldown-max", type=int, default=15, help="Max cooldown between accounts (default: 15)")
    return parser.parse_args()


# ========================================================================
# MAIN ENTRY POINT
# ========================================================================
def main():
    args = parse_args()
    akun_list = load_credentials(args.accounts)

    if not akun_list:
        log(chr(0x274C), R, "Tidak ada akun untuk diproses. Keluar.")
        return

    section("A2E Bot - Mulai", chr(0x1F680))
    log(chr(0x1F512), C, "Proxy: " + str(args.proxy or "None"))
    log(chr(0x1F465), C, "Akun: " + str(len(akun_list)))

    sukses = 0
    gagal = 0
    retry_left = set(i for i in range(len(akun_list)))
    hasil_per_akun = {}

    for attempt in range(args.max_retries):
        if not retry_left:
            break
        current_batch = sorted(retry_left)
        retry_left = set()
        for idx in current_batch:
            cred = akun_list[idx]
            username = cred.get("username", f"akun_{idx}")
            status = proses_akun(
                cred,
                proxy=args.proxy,
                headless=not args.no_headless
            )
            hasil_per_akun[idx] = status

            if status.get("status") == "success":
                sukses += 1
                log(chr(0x2705), G, f"[{attempt+1}] Akun {username} BERHASIL didanai.")
            elif status.get("status") == "retry":
                retry_left.add(idx)
                log(chr(0x1F504), Y, f"[{attempt+1}] Akun {username} RETRY - {status.get('reason')}")
            else:
                gagal += 1
                log(chr(0x274C), R, f"[{attempt+1}] Akun {username} GAGAL - {status.get('reason')}")

        if retry_left and attempt < args.max_retries - 1:
            wait = random.randint(args.cooldown_min, args.cooldown_max)
            log(chr(0x23F0), Y, f"Cooldown {wait}s sebelum retry...")
            time.sleep(wait)

    failed_indices = set(range(len(akun_list))) - set(hasil_per_akun.keys())
    gagal += len(failed_indices)
    for idx in failed_indices:
        hasil_per_akun[idx] = {"status": "fail", "reason": "not_processed", "username": akun_list[idx].get("username")}

    result_box(sukses, gagal, len(akun_list))
    log(chr(0x1F512), C, "Bot selesai.")


if __name__ == "__main__":
    main()
