# How to Add a New Organisation

Buzzowl uses a two-key system to control who can create orgs and who can join them.

- **Registration key** — operator-issued, required to create a new org. You generate these via the CLI.
- **Invite key** — org-admin-issued, required to add new members to an existing org. Generated in the `/settings` UI.

---

## Step 1 — Generate a registration key

Run this on the machine where the server is running:

```bash
source .venv/bin/activate
python scripts/manage_registration.py new --label "Org Name"
```

Example output:

```
── New Registration Key ──────────────────────────────────────────
  ID      : 3
  Key     : aB3xQ7...
  Label   : Acme Corp
  Expires : never

  Share this key with the org admin. They enter it in the
  'Registration key' field on the Register tab.
```

Other useful commands:

```bash
# List all orgs and all registration keys
python scripts/manage_registration.py

# Key that expires in 30 days
python scripts/manage_registration.py new --label "Alpha Tester" --days 30

# Revoke an unused key by its ID
python scripts/manage_registration.py revoke 3
```

---

## Step 2 — Register the org

Share the key with the org's first admin. They:

1. Go to `http://<your-server>`
2. Click the **Register** tab
3. Fill in:
   - **Registration key** — the key you generated
   - **Organisation name** — e.g. `Acme Corp`
   - **Username** — their admin username
   - **Password**
4. Click **Create account**

This creates the org and their admin account in one step. The registration key is consumed — it cannot be reused.

---

## Step 3 — Add more team members (org admin task)

Once the org exists, the admin invites teammates from the `/settings` page — no CLI needed.

1. Log in and go to `/settings` (or click the `⚙` gear icon in the navbar)
2. Scroll to **Generate Invite Key**
3. Optionally enter an email label and select a role, then click **Generate Key**
4. Copy the key and share it with the new team member (Slack, email, Telegram, etc.)

The new member:
1. Goes to the app URL
2. Clicks the **Join** tab on the login screen
3. Pastes the invite key, picks a username, display name, and password
4. Clicks **Join workspace** — they're in

Each invite key is **one-time use** — generate a new one per person.

---

## Reference — key types

| Key type | Who generates | How | One-time? | Purpose |
|---|---|---|---|---|
| Registration key | Operator (you) | `manage_registration.py new` | Yes | Create a new org |
| Invite key | Org admin | `/settings` UI | Yes | Add a member to an existing org |
