# Clipfarmer — Oracle Cloud Always Free deploy

Target shape: **VM.Standard.A1.Flex** (ARM Ampere), 4 OCPUs, 24 GB RAM, Ubuntu 22.04. Always Free — no monthly charge.

## One-time setup

1. **Create Oracle Cloud account** at https://www.oracle.com/cloud/free/. Pick a home region with ARM capacity (Frankfurt or London are usually open; avoid US-Ashburn).
2. **Provision the VM** (full screen-by-screen walkthrough in chat — Chris will be guided):
   - Shape: `VM.Standard.A1.Flex` (ARM, Always Free eligible)
   - 4 OCPUs, 24 GB RAM
   - OS: Canonical Ubuntu 22.04 (ARM)
   - SSH key: paste your laptop's public key, save the private key locally as `~/.ssh/oracle_clipfarmer`
   - Networking: default VCN is fine; the box only needs outbound traffic.
3. **Sync the project from your laptop**:
   ```bash
   VM_IP=<oracle public ip> ./deploy/rsync_from_laptop.sh
   ```
4. **Run the installer on the box**:
   ```bash
   ssh -i ~/.ssh/oracle_clipfarmer ubuntu@<oracle public ip>
   cd ~/clipfarmer && bash deploy/install.sh
   ```
   That installs Python 3.12, Playwright browsers, FFmpeg, sets up the venv, installs systemd units, and starts the scheduler.

## Daily ops

- **Status:** `systemctl status clipfarmer-scheduler`
- **Live log:** `journalctl -u clipfarmer-scheduler -f`
- **Restart after a code update:**
  ```bash
  # On laptop
  VM_IP=<ip> ./deploy/rsync_from_laptop.sh
  # On the box
  sudo systemctl restart clipfarmer-scheduler clipfarmer-bot
  ```

## What lives on the box

- `/home/ubuntu/clipfarmer/` — project root, mirrors the laptop layout
- `.venv/` — created by the installer, never synced
- `.env` and `.auth/` — synced separately so secrets land outside the main rsync
- `data/` and `logs/` — local to the box; not synced back (use a periodic backup if needed)

## Notes

- The ARM shape lacks pip wheels for some legacy libs; PyTorch + Whisper both have aarch64 wheels (verified).
- Oracle never sends a card charge for Always Free instances. Keep your billing-account-level **service limits** at default — they enforce the free-tier ceiling for you.
