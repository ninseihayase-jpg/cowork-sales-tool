"""
開発用サーバー起動スクリプト。
cowork/ ディレクトリの .py ファイルが変更されると自動でサーバーを再起動する。

使い方:
  python3 scripts/run_webapp_dev.py

終了: Ctrl+C
"""
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WATCH_DIR = ROOT / "cowork"
PORT = 8787


def get_mtimes():
    return {f: f.stat().st_mtime for f in WATCH_DIR.glob("**/*.py")}


def start_server():
    print(f"\n[dev] サーバー起動中... http://localhost:{PORT}")
    return subprocess.Popen(
        [sys.executable, str(ROOT / "scripts" / "run_webapp.py")],
        cwd=str(ROOT),
    )


def main():
    print("[dev] 開発サーバー起動。cowork/*.py の変更を監視します。終了: Ctrl+C")
    mtimes = get_mtimes()
    proc = start_server()

    try:
        while True:
            time.sleep(2)
            new_mtimes = get_mtimes()
            changed = [f for f in new_mtimes if new_mtimes[f] != mtimes.get(f)]
            if changed:
                print(f"\n[dev] 変更検出: {[f.name for f in changed]}")
                proc.terminate()
                proc.wait()
                time.sleep(0.5)
                mtimes = new_mtimes
                proc = start_server()
                print("[dev] 再起動完了。")
    except KeyboardInterrupt:
        print("\n[dev] 終了します。")
        proc.terminate()


if __name__ == "__main__":
    main()
