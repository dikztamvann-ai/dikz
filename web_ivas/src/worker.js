// Worker dashboard IVAS — D1 + push sync dari bot Telegram.
// Endpoint:
//   POST /ingest      (x-sync-key)  — 1 event realtime, upsert 1 baris
//   POST /sync        (x-sync-key)  — full-sync bulk tiap 10 menit
//   GET  /api/read    (x-sync-key)  — bot baca data (D1 jadi source of truth)
//   POST /api/write   (x-sync-key)  — operasi atomik (spend/reserve/refund/grant)
//   GET/POST /login                 — login password admin
//   GET  / , /admin  (cookie)       — dashboard HTML
//   GET  /api/stats  (cookie)       — JSON data untuk dashboard

const COOKIE = "ivas_sess";

function json(data, status = 200, extra = {}) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json; charset=utf-8", ...extra },
  });
}

// Sesi ditandatangani HMAC(ADMIN_PASSWORD) supaya tidak bisa dipalsu.
async function signToken(secret, exp) {
  const key = await crypto.subtle.importKey(
    "raw", new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const sig = await crypto.subtle.sign(
    "HMAC", key, new TextEncoder().encode("v1." + exp));
  const b64 = btoa(String.fromCharCode(...new Uint8Array(sig)));
  return `${exp}.${b64}`;
}

async function verifyToken(secret, token) {
  if (!token || !token.includes(".")) return false;
  const exp = token.split(".")[0];
  if (!/^\d+$/.test(exp) || Date.now() > Number(exp)) return false;
  const expect = await signToken(secret, exp);
  // konstan-waktu sederhana
  if (expect.length !== token.length) return false;
  let diff = 0;
  for (let i = 0; i < expect.length; i++) diff |= expect.charCodeAt(i) ^ token.charCodeAt(i);
  return diff === 0;
}

function getCookie(req, name) {
  const c = req.headers.get("cookie") || "";
  const m = c.match(new RegExp("(?:^|; )" + name + "=([^;]+)"));
  return m ? decodeURIComponent(m[1]) : null;
}

// ── Tabel yang boleh disinkron + kolom + primary key ──
const TABLES = {
  allowed_users:    { pk: "user_id",   cols: ["user_id","username","added_by","added_at"] },
  premium_access:   { pk: "user_id",   cols: ["user_id","expired_at","package","days","total_paid","buy_count","invoice_id","granted_by","created_at","updated_at"] },
  premium_tokens:   { pk: "user_id",   cols: ["user_id","balance","total_bought","total_spent","total_refund","updated_at"] },
  token_ledger:     { pk: "id",        cols: ["id","user_id","delta","balance_after","kind","ref","note","created_at"] },
  premium_invoices: { pk: "invoice_id",cols: ["invoice_id","user_id","package","kind","days","tokens","amount","fee","total","status","paid_at","expired_at","created_at"] },
  resetotp_trial:   { pk: "user_id",   cols: ["user_id","used","first_used_at","last_used_at"] },
};

function upsertSQL(table) {
  const t = TABLES[table];
  const cols = t.cols;
  const ph = cols.map(() => "?").join(",");
  const setCols = cols.filter((c) => c !== t.pk);
  const setExpr = setCols.map((c) => `${c}=excluded.${c}`).join(", ");
  return `INSERT INTO ${table} (${cols.join(",")}) VALUES (${ph}) ` +
         `ON CONFLICT(${t.pk}) DO UPDATE SET ${setExpr}`;
}

function rowValues(table, obj) {
  return TABLES[table].cols.map((c) => (obj[c] === undefined ? null : obj[c]));
}

async function upsertRows(env, table, rows) {
  if (!TABLES[table] || !Array.isArray(rows) || rows.length === 0) return 0;
  const sql = upsertSQL(table);
  const stmts = rows.map((r) => env.DB.prepare(sql).bind(...rowValues(table, r)));
  await env.DB.batch(stmts);
  return rows.length;
}

// ── READ API: bot baca data dari D1 ──────────────────────────────
async function readTable(env, table, url) {
  const t = TABLES[table];
  if (!t) return json({ ok: false, error: "bad table" }, 400);

  // Count-only: ?count=1 → hanya jumlah baris, tanpa kirim data.
  if (url.searchParams.get("count") === "1") {
    const r = await env.DB.prepare(`SELECT COUNT(*) as cnt FROM ${table}`).first();
    return json({ ok: true, count: r ? r.cnt : 0 });
  }

  const userId = url.searchParams.get("user_id");
  const pkVal = url.searchParams.get("pk");
  const limitRaw = parseInt(url.searchParams.get("limit") || "0", 10);
  const limit = Number.isFinite(limitRaw) && limitRaw > 0 ? Math.min(limitRaw, 5000) : 0;

  // Lookup 1 baris via primary key (paling sering dipakai bot).
  const single = pkVal !== null ? pkVal : (t.pk === "user_id" ? userId : null);
  if (single !== null && single !== "") {
    const r = await env.DB.prepare(`SELECT * FROM ${table} WHERE ${t.pk} = ?`)
      .bind(single).first();
    return json({ ok: true, row: r || null });
  }

  // Filter per-user untuk tabel yang punya kolom user_id tapi PK-nya lain.
  if (userId && t.cols.includes("user_id") && t.pk !== "user_id") {
    const order = t.pk === "id" ? "id DESC" : "created_at DESC";
    const sql = `SELECT * FROM ${table} WHERE user_id = ? ORDER BY ${order}` +
                (limit ? ` LIMIT ${limit}` : " LIMIT 500");
    const res = await env.DB.prepare(sql).bind(userId).all();
    return json({ ok: true, rows: res.results || [] });
  }

  // Tanpa filter = dump seluruh tabel (dipakai startup restore bot).
  const sql = `SELECT * FROM ${table}` + (limit ? ` LIMIT ${limit}` : "");
  const res = await env.DB.prepare(sql).all();
  return json({ ok: true, rows: res.results || [] });
}

const nowIsoSql = () => new Date().toISOString().slice(0, 19).replace("T", " ");

async function tokenBalance(env, userId) {
  const r = await env.DB.prepare("SELECT balance FROM premium_tokens WHERE user_id = ?")
    .bind(userId).first();
  return r ? (r.balance || 0) : 0;
}

// Baris token lengkap — dikirim balik ke bot supaya bisa di-mirror ke cache lokal.
async function tokenRow(env, userId) {
  return (await env.DB.prepare("SELECT * FROM premium_tokens WHERE user_id = ?")
    .bind(userId).first()) || null;
}

// Catat mutasi token ke ledger. Return id baris baru (atau null).
async function ledgerLog(env, userId, delta, balanceAfter, kind, ref, note, now) {
  try {
    const r = await env.DB.prepare(
      "INSERT INTO token_ledger (user_id, delta, balance_after, kind, ref, note, created_at) " +
      "VALUES (?, ?, ?, ?, ?, ?, ?)")
      .bind(userId, delta, balanceAfter, kind, ref || null, note || null, now).run();
    return r.meta ? r.meta.last_row_id : null;
  } catch { return null; }
}

// ── WRITE API: operasi atomik yang tidak bisa lewat /ingest ──────
async function doWrite(env, body) {
  const action = String(body.action || "");
  const userId = parseInt(body.user_id, 10);
  if (!Number.isFinite(userId)) return json({ ok: false, error: "bad user_id" }, 400);
  const now = nowIsoSql();

  if (action === "add_tokens") {
    const amount = parseInt(body.amount, 10) || 0;
    if (amount <= 0) {
      return json({ ok: true, balance: await tokenBalance(env, userId),
                    row: await tokenRow(env, userId) });
    }
    const kind = body.kind || "buy";
    const bought = (kind === "buy" || kind === "grant") ? amount : 0;
    await env.DB.prepare(
      "INSERT INTO premium_tokens (user_id, balance, total_bought, total_spent, total_refund, updated_at) " +
      "VALUES (?, ?, ?, 0, 0, ?) ON CONFLICT(user_id) DO UPDATE SET " +
      "balance = premium_tokens.balance + excluded.balance, " +
      "total_bought = premium_tokens.total_bought + excluded.total_bought, " +
      "updated_at = excluded.updated_at")
      .bind(userId, amount, bought, now).run();
    const bal = await tokenBalance(env, userId);
    await ledgerLog(env, userId, amount, bal, kind, body.ref, body.note, now);
    return json({ ok: true, balance: bal, row: await tokenRow(env, userId) });
  }

  if (action === "spend_tokens") {
    const amount = parseInt(body.amount, 10) || 0;
    if (amount <= 0) {
      return json({ ok: true, balance: await tokenBalance(env, userId),
                    row: await tokenRow(env, userId) });
    }
    const r = await env.DB.prepare(
      "UPDATE premium_tokens SET balance = balance - ?, " +
      "total_spent = total_spent + ?, updated_at = ? " +
      "WHERE user_id = ? AND balance >= ?")
      .bind(amount, amount, now, userId, amount).run();
    if (!r.meta || r.meta.changes === 0) {
      return json({ ok: false, error: "insufficient",
                    balance: await tokenBalance(env, userId),
                    row: await tokenRow(env, userId) });
    }
    const bal = await tokenBalance(env, userId);
    await ledgerLog(env, userId, -amount, bal, "spend", body.ref, body.note, now);
    return json({ ok: true, balance: bal, row: await tokenRow(env, userId) });
  }

  if (action === "refund_tokens") {
    const amount = parseInt(body.amount, 10) || 0;
    if (amount <= 0) {
      return json({ ok: true, balance: await tokenBalance(env, userId),
                    row: await tokenRow(env, userId) });
    }
    // Idempoten per ref: kalau ref ini sudah pernah di-refund, jangan dobel.
    if (body.ref) {
      const dup = await env.DB.prepare(
        "SELECT 1 FROM token_ledger WHERE ref = ? AND kind = 'refund' LIMIT 1")
        .bind(body.ref).first();
      if (dup) {
        return json({ ok: false, error: "already_refunded",
                      balance: await tokenBalance(env, userId),
                      row: await tokenRow(env, userId) });
      }
    }
    await env.DB.prepare(
      "INSERT INTO premium_tokens (user_id, balance, total_bought, total_spent, total_refund, updated_at) " +
      "VALUES (?, ?, 0, 0, ?, ?) ON CONFLICT(user_id) DO UPDATE SET " +
      "balance = premium_tokens.balance + excluded.balance, " +
      "total_refund = premium_tokens.total_refund + excluded.total_refund, " +
      "updated_at = excluded.updated_at")
      .bind(userId, amount, amount, now).run();
    const bal = await tokenBalance(env, userId);
    await ledgerLog(env, userId, amount, bal, "refund", body.ref, body.note, now);
    return json({ ok: true, balance: bal, row: await tokenRow(env, userId) });
  }

  if (action === "reserve_trial") {
    const limit = parseInt(body.limit, 10) || 1;
    const resetHours = parseInt(body.reset_hours, 10) || 24;
    // Reset counter kalau last_used_at > resetHours jam lalu (daily trial).
    await env.DB.prepare(
      "INSERT OR IGNORE INTO resetotp_trial (user_id, used, first_used_at, last_used_at) " +
      "VALUES (?, 0, ?, ?)").bind(userId, now, now).run();
    await env.DB.prepare(
      "UPDATE resetotp_trial SET used = 0 " +
      "WHERE user_id = ? AND used > 0 AND " +
      "(julianday(?) - julianday(last_used_at)) * 24 >= ?")
      .bind(userId, now, resetHours).run();
    const r = await env.DB.prepare(
      "UPDATE resetotp_trial SET used = used + 1, " +
      "first_used_at = COALESCE(first_used_at, ?), last_used_at = ? " +
      "WHERE user_id = ? AND used < ?")
      .bind(now, now, userId, limit).run();
    const row = await env.DB.prepare("SELECT * FROM resetotp_trial WHERE user_id = ?")
      .bind(userId).first();
    if (!r.meta || r.meta.changes === 0) {
      return json({ ok: false, error: "limit_reached", row: row || null });
    }
    return json({ ok: true, row: row || null });
  }

  if (action === "refund_trial") {
    await env.DB.prepare(
      "UPDATE resetotp_trial SET used = MAX(0, used - 1), last_used_at = ? " +
      "WHERE user_id = ? AND used > 0").bind(now, userId).run();
    const row = await env.DB.prepare("SELECT * FROM resetotp_trial WHERE user_id = ?")
      .bind(userId).first();
    return json({ ok: true, row: row || null });
  }

  if (action === "consume_trial") {
    await env.DB.prepare(
      "INSERT INTO resetotp_trial (user_id, used, first_used_at, last_used_at) " +
      "VALUES (?, 1, ?, ?) ON CONFLICT(user_id) DO UPDATE SET " +
      "used = resetotp_trial.used + 1, last_used_at = excluded.last_used_at")
      .bind(userId, now, now).run();
    const row = await env.DB.prepare("SELECT * FROM resetotp_trial WHERE user_id = ?")
      .bind(userId).first();
    return json({ ok: true, row: row || null });
  }

  if (action === "grant_premium") {
    const days = parseInt(body.days, 10) || 0;
    const amount = parseInt(body.amount, 10) || 0;
    const pkg = body.package || `${days}d`;
    const cur = await env.DB.prepare("SELECT expired_at FROM premium_access WHERE user_id = ?")
      .bind(userId).first();
    // Kalau langganan masih aktif, hari baru ditumpuk di atas sisa.
    const active = cur && cur.expired_at && cur.expired_at > now;
    const baseMs = active ? Date.parse(cur.expired_at.replace(" ", "T") + "Z") : Date.now();
    const newExp = new Date(baseMs + days * 86400000)
      .toISOString().slice(0, 19).replace("T", " ");
    await env.DB.prepare(
      "INSERT INTO premium_access (user_id, expired_at, package, days, total_paid, " +
      "buy_count, invoice_id, granted_by, created_at, updated_at) " +
      "VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET " +
      "expired_at = excluded.expired_at, package = excluded.package, days = excluded.days, " +
      "invoice_id = COALESCE(excluded.invoice_id, premium_access.invoice_id), " +
      "granted_by = excluded.granted_by, " +
      "total_paid = premium_access.total_paid + excluded.total_paid, " +
      "buy_count = premium_access.buy_count + 1, updated_at = excluded.updated_at")
      .bind(userId, newExp, pkg, days, amount,
            body.invoice_id || null, body.granted_by || null, now, now).run();
    const row = await env.DB.prepare("SELECT * FROM premium_access WHERE user_id = ?")
      .bind(userId).first();
    return json({ ok: true, expired_at: newExp, row: row || null });
  }

  return json({ ok: false, error: "unknown action" }, 400);
}

export default {
  async fetch(req, env) {
    const url = new URL(req.url);
    const path = url.pathname;

    // ── SYNC / READ / WRITE: bot ↔ Worker (butuh x-sync-key) ──
    if (path === "/ingest" || path === "/sync" ||
        path === "/api/read" || path === "/api/write") {
      const key = req.headers.get("x-sync-key") || "";
      if (!env.SYNC_KEY || key !== env.SYNC_KEY) return json({ error: "unauthorized" }, 401);

      // Bot baca data — D1 jadi source of truth.
      if (path === "/api/read") {
        if (req.method !== "GET") return json({ error: "GET only" }, 405);
        try {
          return await readTable(env, url.searchParams.get("table") || "", url);
        } catch (e) {
          return json({ ok: false, error: String(e) }, 500);
        }
      }

      if (req.method !== "POST") return json({ error: "POST only" }, 405);
      let body;
      try { body = await req.json(); } catch { return json({ error: "bad json" }, 400); }

      try {
        if (path === "/api/write") {
          return await doWrite(env, body);
        }
        if (path === "/ingest") {
          // { table, row }  atau  { table, rows:[...] }
          const rows = body.rows || (body.row ? [body.row] : []);
          const n = await upsertRows(env, body.table, rows);
          return json({ ok: true, table: body.table, upserted: n });
        } else {
          // full-sync: { tables: { premium_access:[...], ... } }
          const tables = body.tables || {};
          let total = 0;
          for (const t of Object.keys(tables)) total += await upsertRows(env, t, tables[t]);
          return json({ ok: true, upserted: total });
        }
      } catch (e) {
        return json({ error: String(e) }, 500);
      }
    }

    // ── LOGIN ──
    if (path === "/login") {
      if (req.method === "POST") {
        const form = await req.formData();
        const pw = form.get("password") || "";
        if (!env.ADMIN_PASSWORD || pw !== env.ADMIN_PASSWORD) {
          return new Response(loginHTML("Password salah."), {
            status: 401, headers: { "content-type": "text/html; charset=utf-8" } });
        }
        const exp = Date.now() + 12 * 3600 * 1000; // 12 jam
        const tok = await signToken(env.ADMIN_PASSWORD, exp);
        return new Response(null, {
          status: 302,
          headers: {
            "location": "/admin",
            "set-cookie": `${COOKIE}=${encodeURIComponent(tok)}; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=43200`,
          },
        });
      }
      return new Response(loginHTML(""), { headers: { "content-type": "text/html; charset=utf-8" } });
    }

    // ── Halaman & API terlindungi cookie ──
    const authed = await verifyToken(env.ADMIN_PASSWORD || "", getCookie(req, COOKIE));

    if (path === "/api/stats") {
      if (!authed) return json({ error: "unauthorized" }, 401);
      return json(await buildStats(env));
    }

    // ── DOWNLOAD DATABASE: generate premium_bot.db dari D1 ──
    if (path === "/api/download_db") {
      if (!authed) return json({ error: "unauthorized" }, 401);
      try {
        // Query semua data dari D1
        const [premiumAccess, premiumTokens, tokenLedger, premiumInvoices, resetotpTrial] = await Promise.all([
          q(env, "SELECT * FROM premium_access ORDER BY user_id"),
          q(env, "SELECT * FROM premium_tokens ORDER BY user_id"),
          q(env, "SELECT * FROM token_ledger ORDER BY id"),
          q(env, "SELECT * FROM premium_invoices ORDER BY id"),
          q(env, "SELECT * FROM resetotp_trial ORDER BY user_id"),
        ]);

        // Buat SQLite file (simplified format — actual SQLite binary generation
        // would require a full SQLite WASM build. For now, return JSON that
        // can be converted to SQLite by the bot.)
        const dbData = {
          format: "premium_bot_db_v1",
          exported_at: new Date().toISOString(),
          tables: {
            premium_access: premiumAccess,
            premium_tokens: premiumTokens,
            token_ledger: tokenLedger,
            premium_invoices: premiumInvoices,
            resetotp_trial: resetotpTrial,
          }
        };

        return new Response(JSON.stringify(dbData, null, 2), {
          headers: {
            "content-type": "application/json",
            "content-disposition": `attachment; filename="premium_bot_${new Date().toISOString().slice(0,10)}.json"`,
          },
        });
      } catch (e) {
        return json({ ok: false, error: String(e) }, 500);
      }
    }

    if (path === "/" || path === "/admin") {
      if (!authed) return Response.redirect(url.origin + "/login", 302);
      return new Response(dashboardHTML(), { headers: { "content-type": "text/html; charset=utf-8" } });
    }

    return json({ error: "not found" }, 404);
  },
};

async function q(env, sql) {
  try { return (await env.DB.prepare(sql).all()).results || []; }
  catch { return []; }
}

async function buildStats(env) {
  const [users, premium, tokens, ledger, invoices, trial, tokenDaily] = await Promise.all([
    q(env, "SELECT * FROM allowed_users ORDER BY added_at DESC LIMIT 1000"),
    q(env, "SELECT * FROM premium_access ORDER BY updated_at DESC LIMIT 500"),
    q(env, "SELECT * FROM premium_tokens ORDER BY updated_at DESC LIMIT 500"),
    q(env, "SELECT * FROM token_ledger ORDER BY id DESC LIMIT 300"),
    q(env, "SELECT * FROM premium_invoices ORDER BY created_at DESC LIMIT 500"),
    q(env, "SELECT * FROM resetotp_trial ORDER BY last_used_at DESC LIMIT 500"),
    // Agregasi token per-hari: berapa token masuk/keluar tiap tanggal.
    q(env,
      "SELECT substr(created_at,1,10) AS day, " +
      "SUM(CASE WHEN delta>0 THEN delta ELSE 0 END) AS masuk, " +
      "SUM(CASE WHEN delta<0 THEN -delta ELSE 0 END) AS keluar, " +
      "SUM(delta) AS net, COUNT(*) AS tx, MAX(created_at) AS last_at " +
      "FROM token_ledger WHERE created_at IS NOT NULL AND created_at != '' " +
      "GROUP BY day ORDER BY day DESC LIMIT 90"),
  ]);
  const nowIso = new Date().toISOString().slice(0, 19).replace("T", " ");
  const today = new Date().toISOString().slice(0, 10);
  const revenue = invoices.filter((i) => i.status === "paid")
    .reduce((s, i) => s + (i.total || 0), 0);
  const activeSubs = premium.filter((p) => p.expired_at && p.expired_at > nowIso).length;
  const tokenCirc = tokens.reduce((s, t) => s + (t.balance || 0), 0);
  const trialUsed = trial.reduce((s, t) => s + (t.used || 0), 0);
  const todayRow = tokenDaily.find((d) => d.day === today);
  const tokenToday = todayRow ? (todayRow.masuk || 0) : 0;
  return {
    summary: { revenue, activeSubs, tokenCirc, trialUsed,
               paidCount: invoices.filter((i) => i.status === "paid").length,
               userCount: users.length, tokenToday },
    users, premium, tokens, ledger, invoices, trial, tokenDaily,
  };
}

function loginHTML(err) {
  return `<!doctype html><html lang="id"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>IVAS · Login</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700&display=swap');
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:'Space Grotesk',system-ui,sans-serif;background:#f5f1e8;
    color:#111;display:grid;place-items:center;min-height:100vh;
    background-image:radial-gradient(#d9d2c0 1.4px,transparent 1.4px);background-size:22px 22px}
  .card{background:#fff;border:3px solid #111;border-radius:18px;padding:34px;
    width:min(380px,92vw);box-shadow:8px 8px 0 #111}
  .brand{display:inline-flex;align-items:center;gap:10px;background:#ffe14d;
    border:3px solid #111;border-radius:12px;padding:8px 14px;box-shadow:4px 4px 0 #111;
    font-weight:700;font-size:18px;margin-bottom:20px}
  .brand svg{width:22px;height:22px}
  h1{font-size:22px;margin-bottom:4px;font-weight:700}
  p.sub{color:#555;font-size:13px;margin-bottom:22px}
  label{display:block;font-size:12px;font-weight:600;margin-bottom:6px;text-transform:uppercase;letter-spacing:.04em}
  input{width:100%;padding:13px 14px;border-radius:12px;border:3px solid #111;
    background:#f5f1e8;font-size:15px;font-family:inherit;font-weight:500}
  input:focus{outline:none;background:#fff;box-shadow:4px 4px 0 #7cc4ff}
  button{width:100%;margin-top:18px;padding:14px;border:3px solid #111;border-radius:12px;
    background:#7cc4ff;color:#111;font-weight:700;font-size:15px;cursor:pointer;
    font-family:inherit;box-shadow:4px 4px 0 #111;transition:transform .05s,box-shadow .05s}
  button:hover{transform:translate(-2px,-2px);box-shadow:6px 6px 0 #111}
  button:active{transform:translate(2px,2px);box-shadow:2px 2px 0 #111}
  .err{color:#c0392b;font-size:13px;margin-top:12px;min-height:16px;font-weight:600}
</style></head><body>
<form class="card" method="post" action="/login">
  <span class="brand">
    <svg viewBox="0 0 24 24" fill="none" stroke="#111" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
    IVAS
  </span>
  <h1>Dashboard Login</h1>
  <p class="sub">Masuk untuk melihat data user, premium &amp; token.</p>
  <label>Password Admin</label>
  <input type="password" name="password" placeholder="••••••••" autofocus required>
  <button type="submit">Masuk →</button>
  <div class="err">${err ? err : ""}</div>
</form></body></html>`;
}

function dashboardHTML() {
  return `<!doctype html><html lang="id"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>IVAS · Dashboard</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700&display=swap');
  *{box-sizing:border-box;margin:0;padding:0}
  :root{
    --bg:#f5f1e8; --ink:#111; --card:#fff;
    --yellow:#ffe14d; --blue:#7cc4ff; --green:#9ff2c0; --pink:#ffb3d1; --purple:#c9b3ff;
    --sh:5px 5px 0 var(--ink);
  }
  body{font-family:'Space Grotesk',system-ui,sans-serif;background:var(--bg);color:var(--ink);
    background-image:radial-gradient(#d9d2c0 1.4px,transparent 1.4px);background-size:22px 22px}
  .app{display:grid;grid-template-columns:240px 1fr;min-height:100vh}

  /* ── Sidebar ── */
  aside{background:var(--ink);color:#fff;padding:22px 18px;display:flex;flex-direction:column;gap:8px;
    position:sticky;top:0;height:100vh}
  .logo{display:flex;align-items:center;gap:10px;background:var(--yellow);color:var(--ink);
    border:3px solid #fff;border-radius:14px;padding:12px 14px;font-weight:700;font-size:19px;
    box-shadow:4px 4px 0 rgba(255,255,255,.25);margin-bottom:18px}
  .logo svg{width:24px;height:24px}
  .navbtn{display:flex;align-items:center;gap:12px;padding:12px 14px;border-radius:12px;
    border:2px solid transparent;color:#cfcfcf;font-weight:600;font-size:14px;cursor:pointer;
    background:none;font-family:inherit;text-align:left;width:100%;transition:.12s}
  .navbtn svg{width:19px;height:19px;flex:none}
  .navbtn:hover{background:#222;color:#fff}
  .navbtn.on{background:var(--blue);color:var(--ink);border-color:#fff}
  .side-foot{margin-top:auto;font-size:11px;color:#888}
  .side-foot a{color:var(--yellow);text-decoration:none;cursor:pointer}

  /* ── Main ── */
  main{padding:26px 30px;overflow:auto}
  .topbar{display:flex;align-items:center;justify-content:space-between;margin-bottom:22px;flex-wrap:wrap;gap:12px}
  .topbar h1{font-size:26px;font-weight:700}
  .topbar .meta{display:flex;align-items:center;gap:10px}
  .ts{font-size:12px;font-weight:600;background:var(--card);border:2px solid var(--ink);
    border-radius:10px;padding:7px 12px}
  .refresh{display:inline-flex;align-items:center;gap:6px;background:var(--green);border:3px solid var(--ink);
    border-radius:11px;padding:8px 14px;font-weight:700;font-size:13px;cursor:pointer;
    box-shadow:3px 3px 0 var(--ink);font-family:inherit;transition:transform .05s,box-shadow .05s}
  .refresh:hover{transform:translate(-2px,-2px);box-shadow:5px 5px 0 var(--ink)}
  .refresh:active{transform:translate(2px,2px);box-shadow:1px 1px 0 var(--ink)}
  .refresh svg{width:15px;height:15px}
  .download{background:var(--blue);color:var(--ink);text-decoration:none;display:inline-flex;align-items:center;gap:6px;
    border:3px solid var(--ink);border-radius:11px;padding:8px 14px;font-weight:700;font-size:13px;cursor:pointer;
    box-shadow:3px 3px 0 var(--ink);font-family:inherit;transition:transform .05s,box-shadow .05s}
  .download:hover{transform:translate(-2px,-2px);box-shadow:5px 5px 0 var(--ink)}
  .download:active{transform:translate(2px,2px);box-shadow:1px 1px 0 var(--ink)}
  .download svg{width:15px;height:15px}

  /* ── Bento stat cards ── */
  .bento{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:16px;margin-bottom:26px}
  .stat{border:3px solid var(--ink);border-radius:18px;padding:18px;box-shadow:var(--sh);position:relative;overflow:hidden}
  .stat .ic{width:40px;height:40px;border:2.5px solid var(--ink);border-radius:11px;display:grid;place-items:center;
    background:#fff;margin-bottom:14px}
  .stat .ic svg{width:21px;height:21px}
  .stat .k{font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:.05em;opacity:.75}
  .stat .v{font-size:28px;font-weight:700;margin-top:3px;line-height:1.1}
  .stat.c1{background:var(--yellow)} .stat.c2{background:var(--blue)}
  .stat.c3{background:var(--green)} .stat.c4{background:var(--pink)}
  .stat.c5{background:var(--purple)} .stat.c6{background:#fff}

  /* ── Data panel ── */
  .panel{display:none;background:var(--card);border:3px solid var(--ink);border-radius:18px;
    box-shadow:var(--sh);overflow:hidden}
  .panel.on{display:block}
  .panel-head{padding:16px 20px;border-bottom:3px solid var(--ink);display:flex;align-items:center;
    justify-content:space-between;background:#fafafa}
  .panel-head .t{font-weight:700;font-size:16px;display:flex;align-items:center;gap:9px}
  .panel-head .t svg{width:18px;height:18px}
  .count{background:var(--ink);color:#fff;font-size:12px;font-weight:700;padding:3px 10px;border-radius:999px}
  .scroll{overflow:auto;max-height:62vh}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th,td{padding:11px 14px;text-align:left;border-bottom:2px solid #eee;white-space:nowrap}
  th{font-weight:700;font-size:11px;text-transform:uppercase;letter-spacing:.04em;
    position:sticky;top:0;background:var(--yellow);border-bottom:3px solid var(--ink);z-index:2}
  tbody tr:nth-child(even){background:#faf8f2}
  tbody tr:hover{background:var(--blue)}
  .pill{padding:3px 10px;border-radius:999px;font-size:11px;font-weight:700;border:2px solid var(--ink);display:inline-block}
  .pill.paid{background:var(--green)} .pill.pending{background:var(--yellow)}
  .pill.expired,.pill.cancelled{background:var(--pink)}
  .mono{font-family:ui-monospace,monospace}
  .up{color:#0a7d3c;font-weight:700}
  .down{color:#c0392b;font-weight:700}
  .empty{padding:40px;text-align:center;font-weight:600;opacity:.6}
  @media(max-width:720px){.app{grid-template-columns:1fr}aside{position:static;height:auto;flex-direction:row;flex-wrap:wrap}
    .logo{margin-bottom:0}.side-foot{display:none}}
</style></head><body>
<div class="app">
  <aside>
    <div class="logo">
      <svg viewBox="0 0 24 24" fill="none" stroke="#111" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
      IVAS
    </div>
    <nav id="nav"></nav>
    <div class="side-foot">© IVAS Panel<br><a onclick="location.href='/login'">Keluar / ganti sesi</a></div>
  </aside>
  <main>
    <div class="topbar">
      <h1 id="tabTitle">Ringkasan</h1>
      <div class="meta">
        <span class="ts" id="ts">memuat…</span>
        <a href="/api/download_db" class="download" download>
          <svg viewBox="0 0 24 24" fill="none" stroke="#111" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
          Download DB
        </a>
        <button class="refresh" id="sndBtn" onclick="_testNotif()" title="Klik untuk aktifkan/tes suara notifikasi">🔕 Suara: klik untuk aktifkan</button>
        <button class="refresh" onclick="load()">
          <svg viewBox="0 0 24 24" fill="none" stroke="#111" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M21 2v6h-6M3 22v-6h6"/><path d="M21 8A9 9 0 0 0 6 5.3L3 8m0 8a9 9 0 0 0 15 2.7l3-2.7"/></svg>
          Refresh
        </button>
      </div>
    </div>
    <div class="bento" id="cards"></div>
    <div id="panels"></div>
  </main>
</div>
<script>
const rp=(s)=>s==null?"":String(s);
const money=(n)=>"Rp "+(n||0).toLocaleString("id-ID");
const ICONS={
  users:'<path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"/>',
  crown:'<path d="M2 18h20M3 18l2-11 5 5 2-6 2 6 5-5 2 11z"/>',
  coin:'<circle cx="12" cy="12" r="9"/><path d="M9.5 9.5h3.5a2 2 0 1 1 0 4H10m1-6v9"/>',
  book:'<path d="M4 5a2 2 0 0 1 2-2h12v18H6a2 2 0 0 1-2-2z"/><path d="M8 3v18"/>',
  invoice:'<path d="M6 2h9l5 5v15H6z"/><path d="M14 2v6h6M9 13h6M9 17h6"/>',
  flask:'<path d="M9 2h6M10 2v6l-5 9a2 2 0 0 0 1.8 3h10.4a2 2 0 0 0 1.8-3l-5-9V2"/>',
  money:'<rect x="2" y="6" width="20" height="12" rx="2"/><circle cx="12" cy="12" r="2.5"/>',
  check:'<path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><path d="M22 4 12 14.01l-3-3"/>',
  calendar:'<rect x="3" y="4" width="18" height="18" rx="2"/><path d="M16 2v4M8 2v4M3 10h18"/>',
  clock:'<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 3"/>',
};
function icon(name){return '<svg viewBox="0 0 24 24" fill="none" stroke="#111" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">'+(ICONS[name]||'')+'</svg>';}

const TABS=[
  ["users","Users","users",["user_id","username","added_by","added_at"]],
  ["premium","Premium Aktif","crown",["user_id","package","days","expired_at","total_paid","buy_count","updated_at"]],
  ["expired","Premium Expired","clock",["user_id","package","days","expired_at","total_paid","buy_count","updated_at"]],
  ["tokens","Token","coin",["user_id","balance","total_bought","total_spent","total_refund","updated_at"]],
  ["tokenDaily","Token/Hari","calendar",["day","masuk","keluar","net","tx","last_at"]],
  ["ledger","Ledger","book",["id","user_id","delta","balance_after","kind","ref","note","created_at"]],
  ["invoices","Invoice","invoice",["invoice_id","user_id","kind","package","days","tokens","total","status","paid_at","created_at"]],
  ["trial","Trial","flask",["user_id","used","first_used_at","last_used_at"]],
];
const LABEL={user_id:"User ID",username:"Username",added_by:"Added By",added_at:"Ditambahkan",
  package:"Paket",days:"Hari",expired_at:"Kadaluarsa",total_paid:"Total Bayar",buy_count:"Beli",
  updated_at:"Update",balance:"Saldo",total_bought:"Dibeli",total_spent:"Dipakai",total_refund:"Refund",
  delta:"Delta",balance_after:"Saldo Akhir",kind:"Jenis",ref:"Ref",note:"Catatan",created_at:"Dibuat",
  invoice_id:"Invoice",tokens:"Token",total:"Total",status:"Status",paid_at:"Dibayar",
  used:"Terpakai",first_used_at:"Pertama",last_used_at:"Terakhir",id:"ID",
  day:"Tanggal",masuk:"Token Masuk",keluar:"Token Keluar",net:"Net",tx:"Transaksi",last_at:"Terakhir"};

function cell(col,val){
  if(col==="status"){const c=(val||"").toLowerCase();const k=c==="paid"?"paid":c==="pending"?"pending":"expired";
    return '<span class="pill '+k+'">'+rp(val)+'</span>';}
  if(col==="total"||col==="total_paid")return money(val);
  if(col==="user_id"||col==="added_by"||col==="invoice_id")return '<span class="mono">'+rp(val)+'</span>';
  if(col==="username"&&val)return '@'+rp(val);
  if(col==="masuk")return val?'<span class="up">+'+Number(val).toLocaleString("id-ID")+'</span>':'0';
  if(col==="keluar")return val?'<span class="down">-'+Number(val).toLocaleString("id-ID")+'</span>':'0';
  if(col==="net"){const n=Number(val)||0;return '<span class="'+(n>=0?"up":"down")+'">'+(n>=0?"+":"")+n.toLocaleString("id-ID")+'</span>';}
  if(col==="delta"){const n=Number(val)||0;return '<span class="'+(n>=0?"up":"down")+'">'+(n>=0?"+":"")+n.toLocaleString("id-ID")+'</span>';}
  return rp(val);
}
function tbl(cols,rows){
  if(!rows||!rows.length)return '<div class="empty">Belum ada data di sini.</div>';
  let h='<div class="scroll"><table><thead><tr>'+cols.map(c=>'<th>'+(LABEL[c]||c)+'</th>').join('')+'</tr></thead><tbody>';
  for(const r of rows)h+='<tr>'+cols.map(c=>'<td>'+cell(c,r[c])+'</td>').join('')+'</tr>';
  return h+'</tbody></table></div>';
}
let _lastRevenue=null,_lastPaidCount=null,_audioReady=false;
const _notifAudio=new Audio('/notif.mp3');
_notifAudio.volume=0.9;_notifAudio.preload='auto';
// Browser memblokir autoplay sampai ada interaksi user. Unlock audio
// pada interaksi pertama (klik/tekan tombol/sentuh) lalu ingat statusnya.
function _unlockAudio(){
  if(_audioReady)return;
  _notifAudio.play().then(()=>{
    _notifAudio.pause();_notifAudio.currentTime=0;_audioReady=true;
    const b=document.getElementById('sndBtn');if(b)b.textContent='🔔 Suara: ON';
  }).catch(()=>{});
}
['click','keydown','touchstart'].forEach(ev=>document.addEventListener(ev,_unlockAudio,{once:false}));
function _testNotif(){
  _audioReady=true;
  const b=document.getElementById('sndBtn');if(b)b.textContent='🔔 Suara: ON';
  _playNotif('🔔 Notifikasi suara aktif');
}
function _toast(msg){
  let t=document.getElementById('notifToast');
  if(!t){t=document.createElement('div');t.id='notifToast';
    t.style.cssText='position:fixed;top:18px;right:18px;z-index:9999;background:#16a34a;color:#fff;padding:14px 20px;border-radius:12px;font-weight:700;box-shadow:0 8px 24px rgba(0,0,0,.25);transition:opacity .4s;font-size:15px';
    document.body.appendChild(t);}
  t.textContent=msg;t.style.opacity='1';
  clearTimeout(t._h);t._h=setTimeout(()=>{t.style.opacity='0';},4000);
}
function _playNotif(msg){
  try{_notifAudio.currentTime=0;const p=_notifAudio.play();if(p&&p.catch)p.catch(()=>{});}catch(e){}
  _toast(msg);
}
async function load(){
  const res=await fetch('/api/stats');
  if(res.status===401){location.href='/login';return;}
  const d=await res.json(),s=d.summary;
  if(_lastRevenue!==null&&(s.revenue>_lastRevenue||s.paidCount>_lastPaidCount)){
    const naik=s.revenue-_lastRevenue;
    _playNotif('💰 Pendapatan bertambah '+(naik>0?money(naik):'(invoice baru lunas)'));
  }
  _lastRevenue=s.revenue;_lastPaidCount=s.paidCount;

  // Pisahkan premium jadi AKTIF vs EXPIRED (expired_at <= sekarang).
  const nowIso=new Date().toISOString().slice(0,19).replace('T',' ');
  const premAll=d.premium||[];
  d.premium=premAll.filter((p)=>p.expired_at&&p.expired_at>nowIso);
  d.expired=premAll.filter((p)=>!(p.expired_at&&p.expired_at>nowIso));

  document.getElementById('cards').innerHTML=[
    ['c1','users','Total User',s.userCount],
    ['c2','crown','Premium Aktif',s.activeSubs],
    ['c3','money','Pendapatan',money(s.revenue)],
    ['c4','check','Invoice Lunas',s.paidCount],
    ['c5','coin','Token Beredar',(s.tokenCirc||0).toLocaleString('id-ID')],
    ['c6','calendar','Token Hari Ini',(s.tokenToday||0).toLocaleString('id-ID')],
    ['c2','flask','Trial Terpakai',s.trialUsed],
  ].map(([cls,ic,k,v])=>'<div class="stat '+cls+'"><div class="ic">'+icon(ic)+'</div><div class="k">'+k+'</div><div class="v">'+v+'</div></div>').join('');

  const nav=document.getElementById('nav'),pn=document.getElementById('panels');
  nav.innerHTML='';pn.innerHTML='';
  TABS.forEach(([key,label,ic,cols],i)=>{
    const b=document.createElement('button');b.className='navbtn'+(i===0?' on':'');
    b.innerHTML=icon(ic)+'<span>'+label+'</span>';
    b.onclick=()=>{
      [...nav.children].forEach(x=>x.className='navbtn');b.className='navbtn on';
      [...pn.children].forEach(x=>x.className='panel');
      document.getElementById('p_'+key).className='panel on';
      document.getElementById('tabTitle').textContent=label;
    };
    nav.appendChild(b);
    const rows=d[key]||[];
    const div=document.createElement('div');div.id='p_'+key;div.className='panel'+(i===0?' on':'');
    div.innerHTML='<div class="panel-head"><span class="t">'+icon(ic)+label+'</span><span class="count">'+rows.length+'</span></div>'+tbl(cols,rows);
    pn.appendChild(div);
  });
  document.getElementById('ts').textContent='⟳ '+new Date().toLocaleTimeString('id-ID');
}
load();
setInterval(load,30000);
</script></body></html>`;
}

