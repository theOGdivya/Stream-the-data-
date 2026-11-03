# AQ Test Platform in a GitHub Codespace

The platform starts by itself in this codespace (the first start takes a few minutes).

1. **Your login:** open `demo-data/login.txt` (first line: `name:password`).
2. **Open the website:** in the **Ports** tab, click the globe icon next to port **8000**
   ("AQ Test Platform"). You get an `https://…app.github.dev` address.
3. **Share it with your team:** in the Ports tab, right-click port 8000 → **Port visibility** → **Public**.
   Then anyone with the address can open it. They still need a login: add one line `name:password` per
   person to `demo-data/login.txt`, then restart the platform (below).
4. **Record from a webcam:** open the website on the computer with the webcam → **Cameras → Open the recorder**.
   No webcam? On the Cameras page pick **Fake camera with demo monitors**.

Good to know:

- Data is kept inside this codespace (`demo-data/`) until you delete the codespace.
- A codespace **stops after a period of inactivity** (30 minutes by default; up to 4 hours in your GitHub
  settings → Codespaces → Default idle timeout). The website is offline while it's stopped, and starts again when you
  reopen the codespace. For tests that run for days, install it on your server instead (`docs/deploy.md`).
- Restart the platform: in the terminal, run `pkill -f "demo.py --no-browser"; bash .devcontainer/start.sh`.
- What's happening: `tail -f demo-data/codespace.log`.
