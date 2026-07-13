#!/bin/bash
# Ribat Watch 起動用ランチャー（macOS ではダブルクリックで実行可能）。
# このファイルのあるフォルダで簡易ウェブサーバを立て、ブラウザで地図を開く。

cd "$(dirname "$0")"                       # スクリプトのある場所へ移動（絶対パスに依存しない）
echo "Ribat Watch — serving at http://localhost:8777/app/"
python3 -m http.server 8777 >/dev/null 2>&1 &   # ポート8777でサーバを起動（バックグラウンド）
SRV=$!                                     # サーバのプロセスID
sleep 1                                    # 起動を少し待つ
open "http://localhost:8777/app/" 2>/dev/null || true   # 既定ブラウザで開く
echo "Server PID $SRV. Press Ctrl+C to stop."
wait $SRV                                  # Ctrl+C まで待機（サーバを動かし続ける）
