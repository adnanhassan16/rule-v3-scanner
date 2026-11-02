#!/bin/bash
# Rule v3 bot installer — run once on a fresh Ubuntu server (DigitalOcean Droplet), as root.
# Usage (in the Droplet's web Console):
#   bash <(curl -sL https://raw.githubusercontent.com/YOUR-GITHUB-NAME/rule-v3-scanner/main/install.sh)
set -e
read -p "Your GitHub user name: " GH </dev/tty
read -p "Telegram bot token: " TG_TOKEN </dev/tty
read -p "Telegram chat ID: " TG_CHAT </dev/tty
read -p "healthchecks.io ping URL (press Enter to skip): " HC_URL </dev/tty

apt-get update -y
apt-get install -y python3-venv curl
mkdir -p /opt/rulev3 && cd /opt/rulev3
RAW="https://raw.githubusercontent.com/$GH/rule-v3-scanner/main"
curl -sfL "$RAW/scanner.py" -o scanner.py
curl -sfL "$RAW/bot.py" -o bot.py
python3 -m venv venv
venv/bin/pip install -q numpy requests

cat > .env <<EOF
TG_TOKEN=$TG_TOKEN
TG_CHAT=$TG_CHAT
HC_URL=$HC_URL
EOF
chmod 600 .env

cat > /etc/systemd/system/rulev3.service <<EOF
[Unit]
Description=ICT Rule v3 bot
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=/opt/rulev3
EnvironmentFile=/opt/rulev3/.env
ExecStart=/opt/rulev3/venv/bin/python -u bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now rulev3
echo
echo "DONE. You should get '🤖 Rule v3 bot started' on Telegram now."
echo "Check status any time:  systemctl status rulev3"
echo "See live log:           journalctl -u rulev3 -f"
echo "See paper trades:       cat /opt/rulev3/paper_trades.csv"
echo "Update the bot:         bash <(curl -sL $RAW/install.sh)"
