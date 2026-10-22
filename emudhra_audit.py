#!/usr/bin/env python3
"""
emudhra_audit.py — eMudhra 中間CA鍵漏えい(2026-10)に対する CAバンドル棚卸しツール

使い方:
  python3 emudhra_audit.py                      # pip が実際に使うバンドルを自動検査
  python3 emudhra_audit.py /path/to/bundle.pem  # 任意のバンドルを検査
  python3 emudhra_audit.py --all                # 既知の全バンドルを一括検査
  python3 emudhra_audit.py --live               # さらに実際のTLS検証経路を検査

判定:
  SPKI  = 漏えいした中間CAそのもの(信頼ストアに入っていたら即アウト)
  ROOT  = その中間CAに署名しているルート(侵入経路になる)
  NAME  = eMudhra/emSign 関連(要目視)
終了コード: 0=安全 1=要対応 2=危険
"""
import argparse, base64, hashlib, os, re, shutil, subprocess, sys

# 漏えいした中間CAの SubjectPublicKeyInfo SHA-256 (証明書から算出)
BAD_SPKI = {
    "ebed6475c2c2d80fcb199f3fe8dd6e6b3ce60b77eb15d4e42c3dec0c6f5b5105": "EM DV TLS CA - G3B (eMudhra BV)",
    "49dd353dc2b919a3770645117d0e60c16115e7d1bc259eefe734b00ea434fe5f": "EM DV TLS CA - G3C (eMudhra Inc)",
    "e0254c7fcdc265e2f0f8a428dabb75ab4ae039b4872d62f5ee57f0877d5a8a41": "EM EV TLS CA - G3B (eMudhra BV)",
    "5c0660a2b164910d8ff589c18e14552e9e0ec237dcefe2cb736d41a4811f81a0": "EM EV TLS CA - G3C (eMudhra Inc)",
    "43bffa19d6ef651451f6841e019931e96f6f2eaeff28ab4b6f1ce4576de2717b": "EM OV TLS CA - G3B (eMudhra BV)",
    "63ecefee431d49523bc1136c4e72d0206a477086caf606cd5cf025686c109cf8": "EM OV TLS CA - G3C (eMudhra Inc)",
}

# 中間CAに署名しているルート。ここが残っている限り偽造チェーンが通る
BAD_ROOTS = {
    "emsign ecc root ca - g3": "emSign ECC Root CA - G3 (直接の署名元)",
    "aaa certificate services": "AAA Certificate Services (Sectigo相互署名経由)",
}
# 目視確認対象(ルート/中間を問わず)
NAME_HINTS = ("emudhra", "emsign", "em dv tls", "em ov tls", "em ev tls")

C = {"R": "\033[31m", "Y": "\033[33m", "G": "\033[32m", "B": "\033[34m", "D": "\033[2m", "0": "\033[0m"}
if not sys.stdout.isatty():
    C = {k: "" for k in C}


def run(cmd, data=None):
    return subprocess.run(cmd, input=data, capture_output=True, check=False)


def split_pem(blob):
    return re.findall(r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", blob, re.S)


def cert_info(pem):
    b = pem.encode()
    subj = run(["openssl", "x509", "-noout", "-subject", "-nameopt", "RFC2253"], b).stdout.decode().strip()
    subj = subj.replace("subject=", "", 1)
    pub = run(["openssl", "x509", "-noout", "-pubkey"], b).stdout
    der = run(["openssl", "pkey", "-pubin", "-outform", "DER"], pub).stdout
    key = run(["openssl", "x509", "-noout", "-text"], b).stdout.decode(errors="replace")
    algo = "?"
    m = re.search(r"Public Key Algorithm:\s*(\S+)", key)
    if m:
        algo = m.group(1)
    bits = re.search(r"(?:Public-Key|NIST CURVE):\s*\(?(\d+)\s*bit", key)
    return {
        "subject": subj,
        "spki": hashlib.sha256(der).hexdigest() if der else "",
        "algo": algo,
        "bits": bits.group(1) if bits else "?",
    }


def pip_bundle():
    """pip が実際に信頼しているバンドルを解決する"""
    code = (
        "import sys\n"
        "try:\n"
        "    from pip._vendor import certifi\n"
        "    print(certifi.where())\n"
        "except Exception:\n"
        "    print('')\n"
    )
    p = run([sys.executable, "-c", code])
    path = p.stdout.decode().strip()
    if not path:
        import ssl
        path = ssl.get_default_verify_paths().cafile or ""
    return path


def audit(path, verbose=False):
    print(f"\n{C['B']}▶ {path}{C['0']}")
    if not os.path.isfile(path):
        print(f"  {C['Y']}SKIP{C['0']} 見つからない")
        return 0
    blob = open(path, "r", errors="replace").read()
    certs = [c for c in split_pem(blob) if "CERTIFICATE" in c]
    if not certs:
        print(f"  {C['Y']}SKIP{C['0']} PEM証明書を含まない")
        return 0
    print(f"  {C['D']}{len(certs)} 件の証明書{C['0']}")

    bad, warn, notes = [], [], []
    for pem in certs:
        i = cert_info(pem)
        low = i["subject"].lower()
        if i["spki"] in BAD_SPKI:
            bad.append(f"中間CA本体: {BAD_SPKI[i['spki']]}")
            continue
        hit = next((v for k, v in BAD_ROOTS.items() if k in low), None)
        if hit:
            warn.append(f"署名元ルート: {hit}")
            continue
        if any(h in low for h in NAME_HINTS):
            notes.append(f"eMudhra系: {i['subject']}")
        # 小さすぎるEC鍵(=今回の手口と同じ欠陥)を検出
        if i["algo"] == "id-ecPublicKey" and i["bits"] not in ("?", "256", "384", "521"):
            notes.append(f"異常なEC鍵長 {i['bits']}bit: {i['subject']}")

    if verbose:
        for n in notes:
            print(f"  {C['D']}· {n}{C['0']}")

    if bad:
        print(f"  {C['R']}危険{C['0']} 漏えいした中間CAが信頼ストアに入っている")
        for b in bad:
            print(f"      - {b}")
    for w in warn:
        print(f"  {C['Y']}要確認{C['0']} {w}")
    if notes and not verbose:
        print(f"  {C['D']}参考: eMudhra系/異常鍵 {len(notes)} 件 (--verbose で表示){C['0']}")
    if not (bad or warn):
        print(f"  {C['G']}OK{C['0']} 該当なし")
    return 2 if bad else (1 if warn else 0)


def live():
    """実際に偽造チェーンを受け入れるか = 本当の危険度"""
    print(f"\n{C['B']}▶ 実TLS検証テスト{C['0']}")
    host = "dv-g3b-emudhra.we-have-a-problem.com"
    code = f"""
import ssl, socket, sys
try:
    ctx = ssl.create_default_context()
    with socket.create_connection(("{host}", 443), timeout=10) as s:
        with ctx.wrap_socket(s, server_hostname="{host}") as t:
            print("ACCEPT")
except ssl.SSLError as e:
    print("REJECT")
except Exception as e:
    print("ERROR", e)
"""
    out = run([sys.executable, "-c", code]).stdout.decode().strip()
    if out == "ACCEPT":
        print(f"  {C['R']}危険{C['0']} 偽造チェーンを受け入れる → CAストア未更新")
        print("       → ca-certificates を更新し、その後このスクリプトを再実行")
        return 2
    if out == "REJECT":
        print(f"  {C['G']}OK{C['0']} 偽造チェーンを拒否 → このPythonは保護済み")
        return 0
    print(f"  {C['Y']}SKIP{C['0']} テスト不能 ({out})")
    return 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("bundles", nargs="*")
    ap.add_argument("--all", action="store_true", help="既知のバンドルを一括検査")
    ap.add_argument("--live", action="store_true", help="実TLS検証も確認")
    ap.add_argument("--verbose", "-v", action="store_true")
    a = ap.parse_args()

    if not shutil.which("openssl"):
        sys.exit("openssl が必要です")

    paths = list(a.bundles)
    if a.all or not paths:
        cands = [
            pip_bundle(),
            "/etc/ssl/certs/ca-certificates.crt",
            "/etc/pki/tls/certs/ca-bundle.crt",
            "/etc/ssl/cert.pem",
            "/usr/local/share/ca-certificates/internal-ca.crt",
        ]
        for p in cands:
            if p and p not in paths:
                paths.append(p)

    worst = 0
    for p in paths:
        worst = max(worst, audit(p, a.verbose))
    if a.live:
        worst = max(worst, live())

    print()
    if worst == 2:
        print(f"{C['R']}判定: 危険 — 直ちに対応{C['0']}")
    elif worst == 1:
        print(f"{C['Y']}判定: 要対応 — 影響範囲を確認{C['0']}")
    else:
        print(f"{C['G']}判定: この検査範囲では問題なし{C['0']}")
    sys.exit(worst)


if __name__ == "__main__":
    main()
