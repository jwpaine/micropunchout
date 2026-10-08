# Punchout

A cXML punchout "micro store" for suppliers. A buyer starts in their procurement system (SAP Ariba,
Coupa, Jaggaer, …), punches out to a catalog hosted by this app, shops, and the cart is returned to
the procurement system as a `PunchOutOrderMessage` (POOM).

```
Procurement system ── PunchOutSetupRequest (POSR) ──► /cxml/v/{vendor_id}/setup
                                                        │ check secret, find buyer connection,
                                                        │ map POSR fields, create/update shopper
                   ◄── StartPage URL (one-time link) ───┘
Buyer's browser ───► /punchout/start/{token} ──► /shop/{token}       browse the catalog, build a cart
Buyer's browser ◄── auto-submitted POOM ──────── "Return cart to procurement"
        └─────────► procurement system's BrowserFormPost URL
```

Built with FastAPI, SQLite, Jinja2, Pydantic and lxml.

## Contents

- [Quick start](#quick-start)
- [Configuration](#configuration)
- [Concepts](#concepts)
- [Onboarding a buyer](#onboarding-a-buyer)
- [Punchout behavior](#punchout-behavior)
- [Settings and field mapping](#settings-and-field-mapping)
- [Product catalog](#product-catalog)
- [Admin console](#admin-console)
- [Where data is stored](#where-data-is-stored)
- [Authentication](#authentication)
- [Security notes](#security-notes)
- [Database changes](#database-changes)
- [Project layout](#project-layout)
- [Tests](#tests)
- [Production checklist](#production-checklist)
- [Not built yet](#not-built-yet)

## Quick start

Requires Python 3.12+.

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/uvicorn app.main:app --reload
```

Open http://localhost:8000 and log in with **admin / password123** (development only).

The SQLite database (`punchout.db`) is created, and migrated if needed, on startup.

### Try a punchout locally

No procurement system is needed. The admin has a test tool that plays the buyer's side.

1. **Vendors → New vendor.** Copy the shared secret shown. It's only shown once; use
   **Rotate secret** if you lose it.
2. **Products → Import CSV.** Upload `samples/products-sample.csv`.
3. **New connection.** Any buyer name, domain `NetworkID`, identity e.g. `AN01000000001`.
4. **Test punchout.** Choose the connection, paste the secret, start.
5. Shop, then click **Return cart to procurement**. You'll see the POOM the buyer would receive.
6. On the vendor page, the session appears under **Recent punchout sessions**, with the raw
   POSR, the mapped fields and the POOM. The shopper appears under **Shoppers**.

## Configuration

Set with environment variables.

| Variable | Default | Purpose |
|---|---|---|
| `SECRET_KEY` | insecure dev key (warns) | Signs admin session cookies. **Set in production.** |
| `DATABASE_PATH` | `punchout.db` | SQLite file location |
| `PUBLIC_BASE_URL` | the request's own URL | Public HTTPS URL of this app. Set it when running behind a proxy. A vendor or connection can override it with `punchout.base_url`. |
| `SESSION_HTTPS_ONLY` | off | Mark the admin session cookie `Secure`. Turn on in production. |
| `AUTH_PROVIDER` | `static` | Admin login provider (see [Authentication](#authentication)) |

Generate a secret key with:

```bash
python3 -c 'import secrets; print(secrets.token_hex(32))'
```

## Concepts

| Thing | What it is |
|---|---|
| **Vendor** | A supplier whose catalog is hosted here. It has a public **vendor ID** (e.g. `v9d1568365bcc`) used in its punchout URL, a **shared secret**, a product catalog and settings. |
| **Connection** | A buyer organization allowed to punch out to a vendor. It's identified by the `From` credential (domain + identity) in its setup requests. A vendor can have many connections, and each can override the vendor's settings. |
| **Shopper** | A shopper account, created or updated on every punchout from the mapped setup request fields. It's keyed by connection + `user.external_id`. |
| **Punchout session** | One shopping trip, created by a setup request. It holds the raw request, the mapped fields, the cart and the POOM that was sent back. |

## Onboarding a buyer

Give the buyer, or enter in their procurement system:

| Setting | Value |
|---|---|
| Punchout / setup URL | `https://<your host>/cxml/v/<vendor_id>/setup` (shown on the vendor page) |
| Shared secret | The vendor's secret |
| To (supplier) identity | The vendor ID |

Then add a **connection** for the buyer's `From` credential:

- **Ariba:** this is the buyer's network ID (`NetworkID` / `AN…`).
- **Platforms where you dictate the credentials:** give each buyer **its own** identity. Don't
  reuse the vendor ID as the `From` identity, or every buyer will match the same connection.

Setup requests from buyers without an active connection are rejected.

## Punchout behavior

### Setup request (`POST /cxml/v/{vendor_id}/setup`)

1. **Authentication:** the vendor ID in the URL plus the `Sender` shared secret. An unknown
   vendor and a wrong secret get the same `401`.
2. **Buyer:** matched by any `From` credential against the vendor's active connections,
   ignoring case.
3. **Fields:** mapped using the vendor's and connection's [`posr` settings](#setup-request-mapping-posr).
   A missing required field gets a `400`.
4. **Shopper account:** created or updated.
5. **Session:** created. The response contains a `StartPage` URL with a one-time token.

- **Operations:** `create`, `edit` (the cart is restored from `ItemOut`), and `inspect` (view-only).
- **Errors:** returned as a cXML `<Status>` with HTTP 200, as procurement systems expect.

### Shopping (`/shop/{token}`)

- **Link lifetimes:** the StartPage link works once and expires after 5 minutes. A shopping
  session lasts 8 hours.
- **No cookies:** the session token is in the URL, so shopping pages work inside procurement
  iframes.
- **Headers:** shopping pages send `Referrer-Policy: no-referrer` and `Cache-Control: no-store`.

### Return

- **Building the POOM:** cart items are mapped using the [`poom` settings](#cart-mapping-poom).
  The POOM is then stored, and auto-posted from the browser to the buyer's `BrowserFormPost` URL.
- **Single use:** the session closes, so a cart can't be returned twice.
- **Credentials:** `From`/`To` echo the setup request's `To`/`From`. The POOM never contains the
  shared secret.
- **When checkout is blocked:**
  - an item was deactivated during the session;
  - the cart mixes currencies;
  - a required cart mapping is empty.
- **Empty cart:** allowed; it means the shopper cancelled.

Generated documents validate against the cXML 1.2.014 DTD.

## Settings and field mapping

Settings are JSON, layered:

```
defaults (code)  ←  vendor overrides  ←  connection overrides  =  settings for this punchout
```

- **Overrides only:** the vendor (`vendors.config`) and each connection (`connections.config`)
  store only what they change, so one buyer can get a different mapping without affecting the
  vendor's other buyers.
- **Merging:** nested objects merge key by key, and anything else (lists, strings) replaces.
  A partial mapping rule merges into the one below it; for example, adding only `transform`
  keeps the inherited sources.
- **Switching off:** set a mapping to `null`.

### The three settings tabs

The admin edits settings in three separate tabs, for the vendor and for each connection:

| Tab | URL | Edits |
|---|---|---|
| **General** | `…/settings/general` | `punchout`, `return` |
| **Setup request mapping (POSR)** | `…/settings/posr` | `posr`: inbound, from the procurement system |
| **Cart mapping (POOM)** | `…/settings/poom` | `poom`: outbound, back to the procurement system |

Each tab shows only its own settings, each with its value and which layer it came from. Saving a
tab replaces only that section.

### General

| Setting | Default | Meaning |
|---|---|---|
| `punchout.base_url` | none | Public URL used in the punchout URL and StartPage links (e.g. a custom domain). Empty uses the app's URL. |
| `return.form_field` | `cxml-urlencoded` | Post the cart as `cxml-urlencoded` or `cxml-base64` |
| `return.target` | `_self` | `_top` breaks out of an iframe when returning the cart |

### Mapping rules

Both mapping sections use the same rule format:

```json
{
  "from": ["extrinsic:UserEmail", "contact:email"],
  "separator": " ",
  "map": { "1000": "C-10442" },
  "transform": ["trim", "lower", "truncate:100"],
  "default": "",
  "required": false
}
```

| Key | Meaning |
|---|---|
| `from` | Sources, tried in order; the first non-empty value wins. Combine parts with ` + `, e.g. `extrinsic:FirstName + extrinsic:LastName`. |
| `separator` | Joins combined parts (default: a space) |
| `map` | Lookup table. Values not in it pass through unchanged. |
| `transform` | `lower`, `upper`, `trim`, `truncate:<n>`, applied in order |
| `default` | Used if every source is empty |
| `required` | POSR: reject the setup request (cXML 400). POOM: block checkout. |

`value:<text>` is a constant in either direction.

### Setup request mapping (POSR)

Maps the incoming setup request into named fields. They identify the shopper, create or update
the shopper account, and are stored on the session for use in the cart.

**Sources**

| Source | Value |
|---|---|
| `extrinsic:<Name>` | `<Extrinsic name="Name">` in the setup request |
| `contact:name`, `contact:email` | The `endUser` contact, else the first contact |
| `shipto:address_id`, `name`, `street`, `city`, `state`, `postal_code`, `country` | The ship-to address |
| `from:domain`, `from:identity` | The buyer's first `From` credential |
| `value:<text>` | A constant |

**Fields**
- **Naming:** names are yours (lowercase and dotted, e.g. `account.id`).
- **Fields the app uses:**

| Field | Default sources | Used for |
|---|---|---|
| `user.external_id` | `UniqueName`, `UserId`, `User`, `UserEmail` extrinsics, then contact email. **Required.** | Identifies the shopper account. Can't be switched off. |
| `user.email` | `UserEmail` extrinsic, then contact email | Shopper's email |
| `user.name` | `UserPrintableName`, `FirstName + LastName`, contact name, `UniqueName`, email | Shopper's name |
| `ship_to.id` | `shipto:address_id` | Ship-to on the session |

- **Other fields:** kept on the session and in the shopper's `attributes`, and available to the
  cart as `session:<field>`.

**Example**

```json
{
  "user.email":  { "transform": ["lower"] },
  "account.id":  { "from": ["shipto:address_id"], "map": { "1000": "C-10442", "2000": "C-10443" } },
  "cost_center": { "from": ["extrinsic:CostCenter"], "required": true }
}
```

### Cart mapping (POOM)

Maps each cart item into the POOM.

**Always sent, and not mappable:** SKU (`SupplierPartID`), quantity, unit price, currency and
unit of measure. Element order is also fixed, so every POOM stays valid against the cXML DTD.

**Sources**

| Source | Value |
|---|---|
| `product:<field>` | `sku`, `name`, `description`, `unit_price`, `currency`, `uom`, `unspsc`, `manufacturer_name`, `manufacturer_part_id`, `lead_time_days` |
| `session:<field>` | A field mapped from the setup request |
| `value:<text>` | A constant |

**Cart elements**

| Element | Default | Notes |
|---|---|---|
| `item.description` | `product:name` | Falls back to the product name if empty |
| `item.supplier_part_aux_id` | none | `<SupplierPartAuxiliaryID>`; comes back on the buyer's purchase order |
| `item.classification` | `product:unspsc` | Required by cXML; may be empty |
| `item.classification_domain` | `value:UNSPSC` | |
| `item.manufacturer_part_id` | `product:manufacturer_part_id` | Left out when empty |
| `item.manufacturer_name` | `product:manufacturer_name` | Left out when empty |
| `item.lead_time` | `product:lead_time_days` | Left out unless it's a whole number |
| `item.extrinsic.<Name>` | none | A line-level `<Extrinsic name="Name">`; left out when empty |

**Example**

```json
{
  "item.description":          { "from": ["product:sku + product:name"], "separator": " - ", "transform": ["truncate:100"] },
  "item.supplier_part_aux_id": { "from": ["product:sku"] },
  "item.classification":       { "from": ["product:unspsc", "value:44120000"] },
  "item.extrinsic.CostCenter": { "from": ["session:cost_center"] },
  "item.extrinsic.Contract":   { "from": ["value:C-2291"] }
}
```

### Validation

Settings are validated by Pydantic models (`app/punchout_config.py`, `app/mapping.py`) when
they're saved. Errors name the setting, e.g. `posr.user.email.from: 'UserEmail' is not a valid source`.

- **Checked across layers:** a connection's settings are checked together with the vendor's,
  and a vendor change that would break a connection's settings is rejected.
- **Speed:** working out the settings for a punchout takes about 10 µs.
- **Postgres:** the JSON columns map directly to `jsonb`.

## Product catalog

Products belong to a vendor and are imported from CSV under **Products → Import CSV**.

- **Matching:** rows are matched by `sku`. New SKUs are added and existing ones updated.
- **All or nothing:** any invalid row stops the whole import, and every problem is listed with
  its line number.
- **Full sync:** deactivates products missing from the file.
- **No deletes:** products are never deleted, so past carts and orders still resolve.
- **Limits:** files are at most 10 MB, and must be UTF-8 (Excel's "CSV UTF-8" works).
- **Headers:** case-insensitive. Unknown columns are ignored and reported.

| Column | Required | Notes |
|---|---|---|
| `sku` | yes | Unique per vendor, max 100 characters |
| `name` | yes | |
| `unit_price` | yes | Non-negative, max 4 decimal places; stored as an exact decimal |
| `currency` | | ISO code, default `USD` |
| `uom` | | Unit code like `EA`, `BX`, `CS`; default `EA` |
| `description` | | |
| `unspsc` | | 8 digits |
| `manufacturer_name`, `manufacturer_part_id` | | |
| `image_url` | | `http://` or `https://` |
| `lead_time_days` | | Whole number |
| `active` | | `yes`/`no`, `true`/`false`, `1`/`0`; default yes |

A template is under **Products → Download template**. `samples/products-sample.csv` has 20 example
products.

The catalog code is built around a `CatalogSource` interface (`app/catalog/sources.py`). CSV is
the first source; a vendor-API source would plug into the same validation and sync.

## Admin console

All pages need an admin login.

| Page | URL |
|---|---|
| Vendors | `/admin/vendors` |
| Vendor: URL, secret, settings summary, connections, recent sessions | `/admin/vendors/{vendor_id}` |
| Vendor settings tabs | `/admin/vendors/{vendor_id}/settings/{general\|posr\|poom}` |
| Connection settings tabs | `/admin/vendors/{vendor_id}/connections/{id}/settings/{general\|posr\|poom}` |
| Products and CSV import | `/admin/vendors/{vendor_id}/products` |
| Shopper accounts | `/admin/vendors/{vendor_id}/shoppers` |
| Session detail: raw POSR, mapped fields, cart, POOM | `/admin/vendors/{vendor_id}/sessions/{id}` |
| Test punchout | `/admin/vendors/{vendor_id}/test` |

## Where data is stored

Everything is in one SQLite file, `punchout.db`.

| Table | Holds | JSON columns |
|---|---|---|
| `vendors` | Vendor ID, name, secret hash and last 4 characters, timestamps | `config`: vendor settings overrides |
| `connections` | Buyer name, `From` domain + identity, active | `config`: connection settings overrides |
| `products` | The catalog (all CSV fields) | |
| `shoppers` | Connection + external ID, email, name, timestamps | `attributes`: other mapped POSR fields |
| `punchout_sessions` | Tokens, status, operation, buyer cookie, return URL, shopper, raw POSR XML (secret replaced by `****`), timestamps | `fields`: mapped POSR fields. `from_credential`/`to_credential`: echoed back in the POOM. |
| `cart_lines` | Session + product + quantity. Prices are read live from `products`. | |
| `order_messages` | Every POOM exactly as sent, with total, currency and line count | |

Outside the database:
- **Code:** setting defaults (`app/punchout_config.py`).
- **Environment variables:** see [Configuration](#configuration).
- **Admin's browser:** a signed session cookie containing only the user ID.

Never stored:
- plaintext shared secrets (shown once; replaced by `****` in stored setup requests);
- uploaded CSV files (discarded after import);
- setup responses.

## Authentication

Admin login goes through a swappable provider (`app/auth/`):

- **`AuthProvider`:** looks up users by ID; the session stores only the ID.
- **`PasswordAuthProvider`:** adds `authenticate(username, password)`. The bundled
  `StaticAuthProvider` is for development only.

To add one, implement the interface, register it in `app/auth/registry.py` and set
`AUTH_PROVIDER`. Redirect-based providers (OIDC, SAML) should subclass `AuthProvider` and add
their own login and callback routes.

All admin pages require the `admin` role, and admins can see every vendor. There's no
per-vendor access control yet.

## Security notes

- **Incoming XML:** parsed without loading DTDs, fetching from the network or expanding
  entities, which blocks XXE attacks.
- **Shared secrets:** stored hashed (SHA-256), with only the last 4 characters kept for display.
  Pages that show a secret are sent `Cache-Control: no-store`.
- **Unknown vendors:** get the same response as a wrong secret, so vendor IDs can't be probed.
- **Admin login:** redirects only allow same-site paths, the session is reset on login, logout
  is POST-only, and cookies are `SameSite=Lax`.
- **Stored setup requests have the secret removed:** raw setup requests are kept for debugging
  with every `<SharedSecret>` value replaced by `****`.
- **POOMs can be edited in transit:** the POOM passes through the shopper's browser, so
  incoming orders should be checked against the stored POOM.

## Database changes

- **New tables:** add to `SCHEMA` in `app/db.py` (`CREATE TABLE IF NOT EXISTS`).
- **Changes to existing tables or data:** add to `MIGRATIONS`. Each entry is a SQL statement or
  a Python function, and runs once on startup, in order. The version is tracked with SQLite's
  `user_version` (currently 6). Only append; never edit or reorder entries.

## Project layout

```
app/
  main.py              app factory, middleware, routers
  config.py            settings from environment
  db.py                SQLite schema, migrations, connections
  vendors.py           vendors; secret generation and verification
  connections.py       buyer connections
  punchout_config.py   punchout settings: schema, defaults, layering, sections, validation
  mapping.py           mapping rules: sources, transforms, resolution
  auth/                pluggable admin authentication
  catalog/
    products.py        product validation, sync, search
    sources.py         catalog sources (CSV now; vendor APIs later)
  cxml/
    common.py          safe parsing, document building, status responses
    setup.py           PunchOutSetupRequest parsing / response
    order_message.py   PunchOutOrderMessage building
  punchout/
    service.py         setup request handling
    sessions.py        sessions, carts, stored POOMs
    shoppers.py        shopper accounts
    poom.py            cart lines to POOM items via mappings
  routes/              HTTP routes: admin, products, connections/sessions/test, cxml, shop
  templates/           Jinja2 templates (admin/, shop/)
samples/products-sample.csv
tests/
```

## Tests

```bash
.venv/bin/pytest -q
```

Each test uses its own temporary database. The suite covers:
- the full round trip, including edit and inspect;
- setup errors, XXE protection, and one-time and expiring tokens;
- CSV import;
- mapping rules, settings layering and validation, and migrations;
- shopper accounts;
- the admin pages.

## Production checklist

- Serve over HTTPS; procurement systems require it.
- Set `SECRET_KEY`, `PUBLIC_BASE_URL` and `SESSION_HTTPS_ONLY=1`.
- Replace the static admin login provider.
- Back up the SQLite database, or move to Postgres. The JSON columns map to `jsonb`; replace the
  migration list with a migration tool such as Alembic.
- Decide on retention: sessions, carts and POOMs are kept forever.

## Not built yet

- `OrderRequest` (purchase order) intake, matched to stored POOMs via `SupplierPartAuxiliaryID`
- Per-buyer contract pricing and catalog visibility
- A platform settings layer (built-in Ariba and Coupa defaults)
- Per-connection shared secrets and generated buyer identities; checking `To` against the vendor ID
- Settings export/import for moving vendors between environments
- Vendor API catalog sync; live price and stock check at checkout
- Storefront options for vendors with their own website (handoff or cXML gateway)
- Per-vendor admin access
