/**
 * wa_server.js — Standalone Baileys WhatsApp HTTP Service
 * Provides HTTP API for dik.py to interact with WhatsApp
 * 
 * Endpoints:
 *   POST /pair          — Start pairing (body: {userId, phone})
 *   GET  /status/:uid   — Check connection status
 *   GET  /groups/:uid   — Fetch groups (admin only)
 *   POST /citer/:uid/:gid — Run Citer GB (strengthen group)
 *   POST /addmember/:uid/:gid — Add members to group
 *   POST /disconnect/:uid — Disconnect + hapus session dari disk
 *   GET  /accounts/:uid   — List akun WA milik user (utama + cadangan)
 *   POST /switch/:uid     — Ganti akun aktif (body: {accountKey})
 */

const http = require("http");
const fs = require("fs");
const path = require("path");
const baileys = require("@whiskeysockets/baileys");
const {
  makeWASocket,
  fetchLatestBaileysVersion,
  useMultiFileAuthState,
  DisconnectReason,
} = baileys;
// Baileys internal helpers untuk parse raw IQ response (Baileys' getBusinessProfile
// membuang banyak field seperti join_date, cover, profile_options. Kita ambil sendiri.)
const WABinary = baileys.WABinary || require("@whiskeysockets/baileys/lib/WABinary");
const { getBinaryNodeChild, getBinaryNodeChildren } = WABinary;
const makeInMemoryStore = baileys.makeInMemoryStore || null;
const pino = require("pino");

// ═══ CONFIG ═══
const CONFIG = JSON.parse(fs.readFileSync(path.join(__dirname, "wa_config.json"), "utf-8"));
const PORT = CONFIG.port || 3891;
const SESSIONS_DIR = path.resolve(__dirname, CONFIG.sessions_dir || "./wa_sessions");
const CITER_ITERATIONS = CONFIG.citer_iterations || 500;
const ADD_DELAY_MIN = CONFIG.add_delay_min || 3000;
const ADD_DELAY_MAX = CONFIG.add_delay_max || 8000;
const ADD_BATCH_SIZE = CONFIG.add_batch_size || 5;
const ADD_BATCH_PAUSE = CONFIG.add_batch_pause || 90000;
const SENDER_STRENGTHEN = CONFIG.sender_strengthen_rounds || 50;

if (!fs.existsSync(SESSIONS_DIR)) fs.mkdirSync(SESSIONS_DIR, { recursive: true });

// Prevent server crash on unhandled errors
process.on("uncaughtException", (err) => {
  console.error("[UNCAUGHT]", err.message);
});
process.on("unhandledRejection", (err) => {
  console.error("[UNHANDLED]", err?.message || err);
});

// ═══ STATE ═══
const sockets = new Map();  // sessionKey -> {sock, store, connected, waNumber, pairCode, manualClose, ownerId}
const citerJobs = new Map(); // `${uid}_${gid}` -> {progress, total, running}
const addJobs = new Map();   // `${uid}_${gid}` -> {added, failed, total, running, errors}
const cekbioJobs = new Map(); // `${uid}` -> {results, progress, total, running, batch}
const ACTIVE_FILE = path.join(__dirname, "wa_active.json");

// ═══ HELPERS ═══
function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }
function randInt(min, max) { return Math.floor(Math.random() * (max - min + 1)) + min; }

function loadActiveMap() {
  try { return JSON.parse(fs.readFileSync(ACTIVE_FILE, "utf8")); } catch { return {}; }
}
function saveActiveMap(map) {
  try { fs.writeFileSync(ACTIVE_FILE, JSON.stringify(map, null, 2)); } catch (e) {
    console.error("[ACTIVE] save error:", e.message);
  }
}
function ownerOfKey(sessionKey) {
  const k = String(sessionKey);
  const i = k.indexOf("__");
  return i === -1 ? k : k.slice(0, i);
}
function isKeyForUser(sessionKey, telegramUid) {
  const u = String(telegramUid);
  const k = String(sessionKey);
  return k === u || k.startsWith(u + "__");
}
function sessionExistsOnDisk(sessionKey) {
  const dir = getSessionPath(sessionKey);
  try {
    return fs.existsSync(dir) && fs.readdirSync(dir).length > 0;
  } catch {
    return false;
  }
}
function listAccountKeys(telegramUid) {
  const u = String(telegramUid);
  const keys = new Set();
  for (const key of sockets.keys()) {
    if (isKeyForUser(key, u)) keys.add(key);
  }
  try {
    if (fs.existsSync(SESSIONS_DIR)) {
      for (const d of fs.readdirSync(SESSIONS_DIR)) {
        if (!isKeyForUser(d, u)) continue;
        const full = path.join(SESSIONS_DIR, d);
        try {
          if (fs.statSync(full).isDirectory() && fs.readdirSync(full).length > 0) keys.add(d);
        } catch {}
      }
    }
  } catch {}
  return [...keys].sort((a, b) => {
    if (a === u) return -1;
    if (b === u) return 1;
    return a.localeCompare(b);
  });
}
function getActiveKey(telegramUid) {
  const u = String(telegramUid);
  const map = loadActiveMap();
  const preferred = map[u];
  if (preferred && (sockets.has(preferred) || sessionExistsOnDisk(preferred))) {
    return preferred;
  }
  const keys = listAccountKeys(u);
  if (keys.includes(u)) return u;
  if (keys.length) return keys[0];
  return u;
}
function setActiveKey(telegramUid, sessionKey) {
  const map = loadActiveMap();
  map[String(telegramUid)] = String(sessionKey);
  saveActiveMap(map);
}
function nextBackupKey(telegramUid) {
  const u = String(telegramUid);
  let n = 2;
  while (sockets.has(`${u}__${n}`) || sessionExistsOnDisk(`${u}__${n}`)) n++;
  return `${u}__${n}`;
}
function resolveEntry(telegramUid) {
  const key = getActiveKey(telegramUid);
  return { key, entry: sockets.get(key) || null };
}

function parseInnerCert(buf) {
  const result = { name: null, bizType: null };
  let pos = 0;
  try {
    while (pos < buf.length) {
      const tag = buf[pos]; pos++;
      const fieldNum = tag >> 3;
      const wireType = tag & 0x07;
      if (wireType === 0) { while (pos < buf.length && buf[pos] & 0x80) pos++; pos++; }
      else if (wireType === 2) {
        let len = 0, shift = 0;
        while (pos < buf.length) { const b = buf[pos]; pos++; len |= (b & 0x7F) << shift; if (!(b & 0x80)) break; shift += 7; }
        const str = buf.slice(pos, pos + len).toString("utf-8");
        pos += len;
        if (fieldNum === 2) result.bizType = str;
        if (fieldNum === 4) result.name = str;
      } else break;
    }
  } catch {}
  return result;
}

function parseVerifiedCert(certBuf) {
  const result = { name: null, bizType: null };
  try {
    let buf;
    if (Buffer.isBuffer(certBuf)) buf = certBuf;
    else if (certBuf?.type === "Buffer" && Array.isArray(certBuf.data)) buf = Buffer.from(certBuf.data);
    else if (typeof certBuf === "string") buf = Buffer.from(certBuf, "binary");
    else return result;
    let pos = 0;
    while (pos < buf.length) {
      const tag = buf[pos]; pos++;
      const fieldNum = tag >> 3;
      const wireType = tag & 0x07;
      if (wireType === 0) { while (pos < buf.length && buf[pos] & 0x80) pos++; pos++; }
      else if (wireType === 2) {
        let len = 0, shift = 0;
        while (pos < buf.length) { const b = buf[pos]; pos++; len |= (b & 0x7F) << shift; if (!(b & 0x80)) break; shift += 7; }
        const data = buf.slice(pos, pos + len);
        pos += len;
        if (fieldNum === 1) { const inner = parseInnerCert(data); if (inner.name) result.name = inner.name; if (inner.bizType) result.bizType = inner.bizType; }
      } else break;
    }
  } catch {}
  return result;
}

function cleanPhone(phone) {
  let p = String(phone).replace(/[^0-9+]/g, "");
  // Remove leading + if present
  p = p.replace(/^\+/, "");
  // Only auto-prefix 62 if starts with 0 (Indonesian local format)
  if (p.startsWith("0")) p = "62" + p.slice(1);
  return p;
}

function getSessionPath(userId) {
  return path.join(SESSIONS_DIR, String(userId));
}

// ═══ SOCKET MANAGEMENT ═══
async function wipeSession(sessionKey, { logout = true } = {}) {
  const key = String(sessionKey);
  const sessionDir = getSessionPath(key);
  const entry = sockets.get(key);
  const owner = entry?.ownerId || ownerOfKey(key);

  if (entry) {
    entry.manualClose = true;
    entry.connected = false;
    if (logout && entry.sock) {
      try { await entry.sock.logout("Disconnect by user"); } catch {}
      try { entry.sock.end(undefined); } catch {}
    } else if (entry.sock) {
      try { entry.sock.end(undefined); } catch {}
    }
    sockets.delete(key);
  }

  // Hapus session dari disk ("database") agar tidak auto-reconnect
  try {
    if (fs.existsSync(sessionDir)) fs.rmSync(sessionDir, { recursive: true, force: true });
  } catch (e) {
    console.error(`[${key}] wipe dir error:`, e.message);
  }

  // Update active pointer — promote akun cadangan jika ada
  const map = loadActiveMap();
  if (map[owner] === key || !map[owner]) {
    const remaining = listAccountKeys(owner).filter((k) => k !== key);
    if (remaining.length) {
      map[owner] = remaining[0];
      saveActiveMap(map);
      if (!sockets.has(remaining[0]) && sessionExistsOnDisk(remaining[0])) {
        try { await reconnectSocket(remaining[0]); } catch {}
      }
    } else {
      delete map[owner];
      saveActiveMap(map);
    }
  }

  console.log(`[${key}] Session wiped (owner=${owner})`);
  return { wiped: key, owner, remaining: listAccountKeys(owner) };
}

async function createSocket(userId, phoneNumber, { asBackup = false } = {}) {
  const ownerId = String(userId);
  // Akun cadangan: slot baru. Pair pertama / replace: pakai akun aktif (atau primary).
  let uid;
  if (asBackup) {
    uid = nextBackupKey(ownerId);
  } else {
    // Pair ulang akun aktif: wipe dulu agar bersih
    const active = getActiveKey(ownerId);
    if (sockets.has(active) || sessionExistsOnDisk(active)) {
      await wipeSession(active, { logout: true });
      await sleep(400);
    }
    uid = ownerId; // primary slot
  }

  const sessionDir = getSessionPath(uid);
  if (!fs.existsSync(sessionDir)) fs.mkdirSync(sessionDir, { recursive: true });

  // Close existing on same key (safety)
  const existing = sockets.get(uid);
  if (existing?.sock) {
    existing.manualClose = true;
    try { existing.sock.end(undefined); } catch {}
    sockets.delete(uid);
    await sleep(500);
  }

  // Clear old session for fresh pairing
  if (fs.existsSync(sessionDir)) {
    try { fs.rmSync(sessionDir, { recursive: true, force: true }); } catch {}
    fs.mkdirSync(sessionDir, { recursive: true });
  }

  const { state, saveCreds } = await useMultiFileAuthState(sessionDir);
  const { version } = await fetchLatestBaileysVersion();
  const store = makeInMemoryStore ? makeInMemoryStore({ logger: pino().child({ level: "silent" }) }) : null;

  const sock = makeWASocket({
    version,
    keepAliveIntervalMs: 30000,
    printQRInTerminal: false,
    logger: pino({ level: "silent" }),
    auth: state,
    browser: ["Mac OS", "Safari", "10.15.7"],
    getMessage: async () => ({ conversation: "" }),
  });

  sock.ev.on("creds.update", saveCreds);
  if (store) store.bind(sock.ev);

  const entry = {
    sock, store, connected: false, waNumber: phoneNumber,
    pairCode: null, manualClose: false, ownerId,
  };
  sockets.set(uid, entry);
  // Pair pertama / replace → set aktif. Cadangan → tetap di akun aktif lama.
  if (!asBackup) {
    setActiveKey(ownerId, uid);
  }

  // Wait for WA handshake (same as working test_pair.js)
  await sleep(5000);

  // Request pairing code
  let pairCode = null;
  try {
    const cleaned = cleanPhone(phoneNumber);
    console.log(`[${uid}] Requesting pairing code for: ${cleaned}`);
    pairCode = await sock.requestPairingCode(cleaned);
    entry.pairCode = pairCode;
    console.log(`[${uid}] Pairing code: ${pairCode}`);
  } catch (err) {
    console.error(`[${uid}] Pairing code error:`, err.message);
  }

  // Register connection handler AFTER pairing code request
  sock.ev.on("connection.update", async (update) => {
    const { connection, lastDisconnect } = update;
    const live = sockets.get(uid);
    if (!live || live.sock !== sock) return;

    if (connection === "open") {
      live.connected = true;
      const waUser = sock.user;
      live.waNumber = waUser?.id?.split(":")[0] || phoneNumber;
      console.log(`[${uid}] WA Connected: ${live.waNumber}`);
    }

    if (connection === "close") {
      live.connected = false;
      if (live.manualClose) {
        sockets.delete(uid);
        try { fs.rmSync(sessionDir, { recursive: true, force: true }); } catch {}
        console.log(`[${uid}] Manual disconnect — session cleared`);
        return;
      }
      const statusCode = lastDisconnect?.error?.output?.statusCode;
      const loggedOut = statusCode === DisconnectReason.loggedOut || statusCode === 401;

      if (loggedOut) {
        sockets.delete(uid);
        try { fs.rmSync(sessionDir, { recursive: true, force: true }); } catch {}
        console.log(`[${uid}] Logged out, session cleared`);
        const map = loadActiveMap();
        if (map[live.ownerId] === uid) {
          const remaining = listAccountKeys(live.ownerId).filter((k) => k !== uid);
          if (remaining.length) map[live.ownerId] = remaining[0];
          else delete map[live.ownerId];
          saveActiveMap(map);
        }
      } else {
        console.log(`[${uid}] Disconnected (code: ${statusCode}), reconnecting...`);
        setTimeout(() => {
          const cur = sockets.get(uid);
          if (cur?.manualClose) return;
          reconnectSocket(uid);
        }, 5000);
      }
    }
  });

  return { pairCode, sessionKey: uid, ownerId };
}

async function reconnectSocket(userId) {
  const uid = String(userId);
  const sessionDir = getSessionPath(uid);
  if (!fs.existsSync(sessionDir) || fs.readdirSync(sessionDir).length === 0) return;

  // Jangan reconnect kalau lagi di-wipe manual
  const existing = sockets.get(uid);
  if (existing?.manualClose) return;

  const { state, saveCreds } = await useMultiFileAuthState(sessionDir);
  const { version } = await fetchLatestBaileysVersion();
  const store = makeInMemoryStore({ logger: pino().child({ level: "silent" }) });

  const sock = makeWASocket({
    version,
    keepAliveIntervalMs: 30000,
    printQRInTerminal: false,
    logger: pino({ level: "silent" }),
    auth: state,
    browser: ["Mac OS", "Safari", "10.15.7"],
    getMessage: async () => ({ conversation: "" }),
  });

  sock.ev.on("creds.update", saveCreds);
  store.bind(sock.ev);

  const ownerId = ownerOfKey(uid);
  const entry = {
    sock, store, connected: false, waNumber: null,
    pairCode: null, manualClose: false, ownerId,
  };
  sockets.set(uid, entry);

  sock.ev.on("connection.update", async (update) => {
    const { connection, lastDisconnect } = update;
    const live = sockets.get(uid);
    if (!live || live.sock !== sock) return;

    if (connection === "open") {
      live.connected = true;
      live.waNumber = sock.user?.id?.split(":")[0];
      console.log(`[${uid}] Reconnected: ${live.waNumber}`);
    }
    if (connection === "close") {
      live.connected = false;
      if (live.manualClose) {
        sockets.delete(uid);
        try { fs.rmSync(sessionDir, { recursive: true, force: true }); } catch {}
        return;
      }
      const statusCode = lastDisconnect?.error?.output?.statusCode;
      if (statusCode === DisconnectReason.loggedOut || statusCode === 401) {
        sockets.delete(uid);
        try { fs.rmSync(sessionDir, { recursive: true, force: true }); } catch {}
      } else {
        setTimeout(() => {
          const cur = sockets.get(uid);
          if (cur?.manualClose) return;
          reconnectSocket(uid);
        }, 10000);
      }
    }
  });
}

// ═══ PROTECT GROUP (formerly Citer GB) ═══
// Sets protective settings ONE TIME. Does NOT toggle/spam.
// This makes the group resistant to mass reports and bans.
// Methods:
//   1. Lock group info (only admins edit)
//   2. Enable join approval (members need approval to join)
//   3. Set only admins can send (announcement mode) - optional
//   4. Get fresh invite code
//   5. Verify settings applied
async function citerGb(userId, groupId) {
  const owner = String(userId);
  const { key: uid, entry } = resolveEntry(owner);
  if (!entry?.connected) return { error: "not_connected" };

  const jobKey = `${owner}_${groupId}`;
  if (citerJobs.get(jobKey)?.running) return { error: "already_running" };

  const sock = entry.sock;

  // CHECK ADMIN FIRST - don't run on groups where we're not admin
  try {
    const meta = await sock.groupMetadata(groupId);
    const myNum = sock.user?.id?.split(":")[0];
    const myLid = sock.user?.lid?.split(":")[0]?.split("@")[0] || "";
    const me = meta.participants?.find(p => {
      const pNum = p.id?.split("@")[0]?.split(":")[0];
      return pNum === myNum || (myLid && pNum === myLid);
    });
    if (!me || (me.admin !== "admin" && me.admin !== "superadmin")) {
      return { error: "not_admin" };
    }
  } catch (e) {
    return { error: "metadata_failed: " + (e.message || "").slice(0, 30) };
  }

  const total = 3; // Only 3 safe steps (NO announcement toggle)
  const job = { progress: 0, total, running: true, error: null };
  citerJobs.set(jobKey, job);

  (async () => {
    try {
      const results = [];

      // Step 1: Lock group (only admins can edit group info)
      try {
        await sock.groupSettingUpdate(groupId, "locked");
        results.push("locked:ok");
        console.log(`[${uid}] Protect: locked ✓`);
      } catch (e) { results.push(`locked:${e.message?.slice(0,20)}`); }
      job.progress = 1;
      await sleep(2000);

      // Step 2: Enable join approval (admin must approve new members)
      try {
        await sock.groupJoinApprovalMode(groupId, "on");
        results.push("approval:ok");
        console.log(`[${uid}] Protect: join approval ON ✓`);
      } catch (e) { results.push(`approval:${e.message?.slice(0,20)}`); }
      job.progress = 2;
      await sleep(2000);

      // Step 3: Verify settings
      try {
        const meta = await sock.groupMetadata(groupId);
        const locked = meta.restrict ? "yes" : "no";
        results.push(`verify:locked=${locked},members=${meta.participants?.length}`);
        console.log(`[${uid}] Protect verify: locked=${locked}, members=${meta.participants?.length}`);
      } catch (e) { results.push(`verify:${e.message?.slice(0,20)}`); }
      job.progress = 3;

      job.error = results.join("|");
      console.log(`[${uid}] Protect done: ${results.join("|")}`);
    } catch (err) {
      job.error = err.message;
    } finally {
      job.running = false;
    }
  })();

  return { status: "started", total };
}

// ═══ ADD MEMBER ═══
// Strategy: Direct add → track actual result (added/invited/failed)
// If account_reachout_restricted → send invite link (count as "invited" not "added")
async function addMembers(userId, groupId, phones) {
  const owner = String(userId);
  const { key: uid, entry } = resolveEntry(owner);
  if (!entry?.connected) return { error: "not_connected" };

  const jobKey = `${owner}_${groupId}`;
  if (addJobs.get(jobKey)?.running) return { error: "already_running" };

  const sock = entry.sock;

  const allPhones = phones.map(p => {
    const cleaned = cleanPhone(p);
    return { phone: cleaned, jid: `${cleaned}@s.whatsapp.net` };
  });

  let existingMembers = new Set();
  try {
    const meta = await sock.groupMetadata(groupId);
    existingMembers = new Set(meta.participants.map(p => p.id.split("@")[0].split(":")[0]));
  } catch {}

  const toAdd = allPhones.filter(p => !existingMembers.has(p.phone));
  const skipped = allPhones.length - toAdd.length;

  // added = actually in group, invited = sent link (need to click)
  const job = { added: 0, invited: 0, failed: 0, total: toAdd.length, running: true, errors: [], skipped };
  addJobs.set(jobKey, job);

  let inviteCode = null;
  try { inviteCode = await sock.groupInviteCode(groupId); } catch {}

  (async () => {
    try {
      for (let i = 0; i < toAdd.length; i++) {
        if (!job.running) break;

        const { jid, phone } = toAdd[i];

        try {
          const result = await sock.groupParticipantsUpdate(groupId, [jid], "add");
          const status = result?.[0]?.status;
          console.log(`[${uid}] Add ${phone}: status=${status}`);

          if (status == 200 || status == "200") {
            job.added++;
          } else if (status == 409 || status == "409") {
            job.added++; // already in group
          } else if (status == 403 || status == "403") {
            // Privacy: send invite link
            if (inviteCode) {
              try {
                await sock.sendMessage(jid, { text: `https://chat.whatsapp.com/${inviteCode}` });
                job.invited++;
              } catch { job.failed++; job.errors.push({ phone, error: "403_dm" }); }
            } else { job.failed++; job.errors.push({ phone, error: "403" }); }
          } else {
            job.failed++;
            job.errors.push({ phone, error: `${status || "unknown"}` });
          }
        } catch (err) {
          const errMsg = err.message || "";
          console.log(`[${uid}] Add ${phone} error: ${errMsg.slice(0,50)}`);

          if (errMsg.includes("reachout_restricted") || errMsg.includes("restricted")) {
            // Send invite link as fallback
            if (inviteCode) {
              try {
                await sock.sendMessage(jid, { text: `https://chat.whatsapp.com/${inviteCode}` });
                job.invited++;
              } catch { job.failed++; job.errors.push({ phone, error: "restricted_dm" }); }
            } else { job.failed++; job.errors.push({ phone, error: "restricted" }); }
          } else {
            job.failed++;
            job.errors.push({ phone, error: errMsg.slice(0, 30) });
          }

          if (errMsg.includes("rate") || errMsg.includes("429")) {
            await sleep(ADD_BATCH_PAUSE);
          }
        }

        await sleep(randInt(ADD_DELAY_MIN, ADD_DELAY_MAX));
        if ((i + 1) % ADD_BATCH_SIZE === 0 && i < toAdd.length - 1) {
          await sleep(ADD_BATCH_PAUSE);
        }
      }
    } catch (err) {
      job.errors.push({ phone: "global", error: err.message });
    } finally {
      job.running = false;
    }
  })();

  return { status: "started", total: toAdd.length, skipped, valid: toAdd.length };
}

// ═══ CREATE GROUP ═══
async function createGroup(userId, name, phones) {
  const { key: uid, entry } = resolveEntry(userId);
  if (!entry?.connected) return { error: "not_connected" };

  const sock = entry.sock;
  const jids = phones.map(p => `${cleanPhone(p)}@s.whatsapp.net`);

  try {
    const result = await sock.groupCreate(name, jids);
    console.log(`[${uid}] Created group: ${result.id} (${name})`);

    // Apply protection settings
    await sleep(1000);
    try { await sock.groupSettingUpdate(result.id, "locked"); } catch {}
    try { await sock.groupJoinApprovalMode(result.id, "on"); } catch {}

    // Promote all members to admin — use actual participant IDs from group metadata
    await sleep(2000);
    try {
      const meta = await sock.groupMetadata(result.id);
      const myJid = sock.user?.id || "";
      const myNum = myJid.split(":")[0].split("@")[0];
      
      const toPromote = (meta.participants || []).filter(p => {
        const pNum = (p.id || "").split("@")[0].split(":")[0];
        return pNum !== myNum && !p.admin;
      });

      console.log(`[${uid}] Promoting ${toPromote.length} participants to admin`);
      
      if (toPromote.length > 0) {
        const promoteIds = toPromote.map(p => p.id);
        try {
          await sock.groupParticipantsUpdate(result.id, promoteIds, "promote");
          console.log(`[${uid}] Promoted all: ${promoteIds.join(", ")}`);
        } catch (e) {
          console.log(`[${uid}] Batch promote failed: ${e.message?.slice(0,50)}, trying one by one`);
          for (const pid of promoteIds) {
            try {
              await sock.groupParticipantsUpdate(result.id, [pid], "promote");
              console.log(`[${uid}] Promoted ${pid}`);
            } catch (e2) {
              console.log(`[${uid}] Promote ${pid} failed: ${e2.message?.slice(0,30)}`);
            }
            await sleep(500);
          }
        }
      }
    } catch (e) {
      console.log(`[${uid}] Metadata/promote error: ${e.message?.slice(0,50)}`);
    }

    return {
      success: true,
      groupId: result.id,
      subject: result.subject || name,
      participants: result.participants?.length || 0,
    };
  } catch (err) {
    console.error(`[${uid}] Create group error:`, err.message);
    return { error: err.message };
  }
}

// ═══ RAW BIZ PROFILE FETCHER ═══
// Baileys' getBusinessProfile hanya ambil 7 field (wid, address, description, website,
// email, category, business_hours). Fungsi ini kirim IQ query yang sama tapi parse
// SEMUA child nodes termasuk profile_options (join_date), cover_photo, verified_level.
async function fetchFullBizProfile(sock, jid) {
  try {
    const iqResp = await sock.query({
      tag: "iq",
      attrs: { to: "s.whatsapp.net", xmlns: "w:biz", type: "get" },
      content: [{
        tag: "business_profile",
        attrs: { v: "244" },
        content: [{ tag: "profile", attrs: { jid } }],
      }],
    });

    const profileNode = getBinaryNodeChild(getBinaryNodeChild(iqResp, "business_profile"), "profile");
    if (!profileNode) return null;

    const out = { attrs: profileNode.attrs || {}, children: {} };
    // Walk semua child nodes, simpan text content dan attrs
    for (const child of (profileNode.content || [])) {
      if (!child || !child.tag) continue;
      const tag = child.tag;
      const text = child.content ? (Buffer.isBuffer(child.content) ? child.content.toString() : (typeof child.content === "string" ? child.content : null)) : null;
      const entry = { attrs: child.attrs || {}, text, subChildren: [] };
      if (Array.isArray(child.content)) {
        for (const sub of child.content) {
          if (!sub || !sub.tag) continue;
          const subText = sub.content ? (Buffer.isBuffer(sub.content) ? sub.content.toString() : (typeof sub.content === "string" ? sub.content : null)) : null;
          entry.subChildren.push({ tag: sub.tag, attrs: sub.attrs || {}, text: subText });
        }
      }
      // Multi-tag support (e.g. multiple business_hours_config)
      if (out.children[tag]) {
        if (!Array.isArray(out.children[tag])) out.children[tag] = [out.children[tag]];
        out.children[tag].push(entry);
      } else {
        out.children[tag] = entry;
      }
    }
    return out;
  } catch (e) {
    return null;
  }
}

// ═══ CEK BIO ═══
async function cekBio(userId, phones, batch = 50) {
  const owner = String(userId);
  const { key: uid, entry } = resolveEntry(owner);
  if (!entry?.connected) return { error: "not_connected" };

  if (cekbioJobs.has(owner) && cekbioJobs.get(owner).running) {
    return { error: "already_running" };
  }

  const sock = entry.sock;
  const jids = phones.map(p => `${cleanPhone(p)}@s.whatsapp.net`);

  const job = { results: [], progress: 0, total: jids.length, running: true, batch };
  cekbioJobs.set(owner, job);

  // Run in background
  (async () => {
    const sem = { count: 0, max: batch };

    async function checkOne(jid, phone) {
      const result = { number: phone, exists: false, bio: null, bioSetAt: null, type: null, name: null, isBusiness: false, tier: null, isSuite: false, description: null, category: null, email: null, timezone: null, catalogCount: 0, cover: null, bizSince: null, hasAiAgent: false, aiAgentInfo: null };
      try {
        const [onWa] = await sock.onWhatsApp(jid);
        if (!onWa?.exists) { job.results.push(result); job.progress++; return; }
        result.exists = true;

        const [statusRes, bizRes, rawBizRes] = await Promise.allSettled([
          sock.fetchStatus(jid).catch(() => null),
          sock.getBusinessProfile(jid).catch(() => null),
          fetchFullBizProfile(sock, jid).catch(() => null),
        ]);

        // Bio
        const st = statusRes.status === "fulfilled" ? statusRes.value : null;
        if (st?.status) {
          result.bio = st.status;
          const raw = st.setAt || st.t || null;
          if (raw) {
            const ts = Number(raw);
            const d = new Date(ts < 1e12 ? ts * 1000 : ts);
            if (!isNaN(d.getTime())) {
              const dd = String(d.getDate()).padStart(2,"0");
              const mm = String(d.getMonth()+1).padStart(2,"0");
              const hh = String(d.getHours()).padStart(2,"0");
              const mi = String(d.getMinutes()).padStart(2,"0");
              const ss = String(d.getSeconds()).padStart(2,"0");
              result.bioSetAt = `${dd}/${mm}/${d.getFullYear()} ${hh}:${mi}:${ss}`;
            }
          }
        }

        // Business profile — merge Baileys getBusinessProfile + raw IQ query
        const biz = bizRes.status === "fulfilled" ? bizRes.value : null;
        const rawBiz = rawBizRes.status === "fulfilled" ? rawBizRes.value : null;

        if (rawBiz) {
          console.log(`[CekBio RAW] ${phone} biz children:`, Object.keys(rawBiz.children).join(","));
        }

        const hasBiz = (biz && typeof biz === "object" && Object.keys(biz).length > 0)
                    || (rawBiz && Object.keys(rawBiz.children || {}).length > 0);

        if (hasBiz) {
          if (biz) console.log(`[CekBio DEBUG] ${phone} getBusinessProfile:`, JSON.stringify(biz).slice(0, 800));
          result.isBusiness = true;
          result.type = "wa_bisnis";

          // From Baileys filtered response
          if (biz) {
            result.name = biz.verifiedName || biz.vname || biz.business_name || biz.name || biz.pushname || null;
            result.description = biz.description || biz.desc || null;
            const rawCat = biz.category || biz.business_category || biz.businessCategory;
            result.category = (typeof rawCat === "object") ? (rawCat?.name || rawCat?.localizedName) : rawCat || null;
            result.email = biz.email || null;
            const bizHours = biz.business_hours || biz.businessHours;
            if (bizHours?.timezone) result.timezone = bizHours.timezone;
            const catCount = Number(biz.catalog_count || biz.catalogCount || biz.catalogue_count || 0);
            if (catCount > 0) result.catalogCount = catCount;
          }

          // From raw IQ response — Baileys drops these fields silently
          if (rawBiz) {
            const kids = rawBiz.children;

            // === bizSince PRIMARY: member_since_ts (WA's own registration timestamp) ===
            const msTs = kids.member_since_ts;
            const msText = kids.member_since_text;
            const months = ["January","February","March","April","May","June","July","August","September","October","November","December"];

            if (msTs?.text) {
              const n = Number(msTs.text.trim());
              if (!isNaN(n) && n > 1000000000) {
                const d = new Date(n < 1e12 ? n * 1000 : n);
                if (!isNaN(d.getTime())) {
                  result.bizSince = `Joined in ${months[d.getMonth()]}, ${d.getFullYear()}`;
                  console.log(`[CekBio RAW] ${phone} bizSince from member_since_ts=${n}: ${result.bizSince}`);
                }
              }
            }
            // Fallback: member_since_text is pre-formatted ("Joined in March, 2019")
            if (!result.bizSince && msText?.text) {
              result.bizSince = msText.text.trim();
              console.log(`[CekBio RAW] ${phone} bizSince from member_since_text: ${result.bizSince}`);
            }

            // Legacy fallbacks (in case WA reverts to old schema)
            if (!result.bizSince) {
              const po = kids.profile_options || kids.biz_profile_options;
              const findTs = (obj) => {
                if (!obj) return null;
                for (const k of ["join_date","privacy_mode_ts","creation_time","creation_ts","created_ts","t","ts","timestamp"]) {
                  if (obj[k] != null && obj[k] !== "" && obj[k] !== "0") {
                    const n = Number(obj[k]);
                    if (!isNaN(n) && n > 1000000000) return n;
                  }
                }
                return null;
              };
              let ts = null;
              if (po) {
                ts = findTs(po.attrs);
                if (!ts && po.subChildren) {
                  for (const sub of po.subChildren) {
                    ts = findTs(sub.attrs);
                    if (ts) break;
                    if (sub.text && /^\d{9,13}$/.test(sub.text.trim())) {
                      const n = Number(sub.text.trim());
                      if (n > 1000000000) { ts = n; break; }
                    }
                  }
                }
              }
              if (!ts) ts = findTs(rawBiz.attrs);
              if (ts) {
                const d = new Date(ts < 1e12 ? ts * 1000 : ts);
                if (!isNaN(d.getTime())) {
                  result.bizSince = `Joined in ${months[d.getMonth()]}, ${d.getFullYear()}`;
                }
              }
            }

            // === AI Agent PRIMARY: automated_type / calling_automated_type ===
            // WA returns automated_type for ALL business accounts with values like:
            //   "none"     — no automation (regular biz)
            //   "standard" — standard biz (auto-reply/away msg, NOT AI agent)
            //   "away"     — away messages enabled (NOT AI agent)
            //   "ai" / "meta_ai" / "agent" / "chatbot" / "assistant" — actual AI Agent
            // Use WHITELIST of known AI values, not blacklist of non-AI values.
            const autoType = kids.automated_type;
            const callAuto = kids.calling_automated_type;
            const AI_VALUES = new Set(["ai","meta_ai","meta-ai","agent","chatbot","chat_bot","assistant","bot","ai_agent","ai-agent","ai_assistant"]);
            const looksLikeAI = (n) => {
              if (!n) return false;
              const t = (n.text || "").trim().toLowerCase().replace(/\s+/g, "_");
              if (!t) return false;
              if (AI_VALUES.has(t)) return true;
              // Fuzzy: contains "ai" as word or "bot"/"agent"/"assistant" substring
              if (/(^|_)ai(_|$)/.test(t)) return true;
              if (t.includes("bot") || t.includes("agent") || t.includes("assistant") || t.includes("gpt") || t.includes("llm")) return true;
              return false;
            };
            if (autoType?.text || callAuto?.text) {
              console.log(`[CekBio RAW] ${phone} automated_type="${autoType?.text || "-"}" calling_automated="${callAuto?.text || "-"}"`);
            }
            if (looksLikeAI(autoType) || looksLikeAI(callAuto)) {
              result.hasAiAgent = true;
              const typeStr = autoType?.text?.trim() || callAuto?.text?.trim() || "business_ai";
              result.aiAgentInfo = {
                is_bot: true,
                bot_type: typeStr,
                capability: callAuto?.text?.trim() || null,
                enabled: true,
              };
              console.log(`[CekBio RAW] ${phone} AI Agent CONFIRMED: automated_type=${autoType?.text} calling=${callAuto?.text}`);
            }

            // Legacy bot node fallback (dedicated <bot> / <ai_agent> node — rare)
            const botNode = kids.bot || kids.ai_agent || kids.automation;
            if (botNode && !result.hasAiAgent) {
              // Only trust bot node if attrs indicate actual AI, not just automation
              const ba = botNode.attrs || {};
              const btype = String(ba.type || ba.bot_type || "").toLowerCase();
              if (btype && (AI_VALUES.has(btype) || btype.includes("ai") || btype.includes("agent"))) {
                result.hasAiAgent = true;
                result.aiAgentInfo = { is_bot: true, bot_type: btype, capability: null, enabled: true };
                console.log(`[CekBio RAW] ${phone} AI Agent from bot node:`, JSON.stringify(ba));
              }
            }

            // === cover_photo / cover node ===
            const coverNode = kids.cover_photo || kids.cover || kids.cover_picture;
            if (coverNode) {
              const url = coverNode.attrs?.url || coverNode.attrs?.direct_path || coverNode.text
                       || coverNode.subChildren?.find(s => s.attrs?.url)?.attrs?.url;
              if (url) result.cover = url;
              console.log(`[CekBio RAW] ${phone} cover:`, url || "(node present, no url)");
            }

            // === biz_identity_info: category / verified level fallback ===
            const bii = kids.biz_identity_info;
            if (bii) {
              const actual = bii.attrs?.actual_actors || bii.subChildren?.find(s => s.tag === "actual_actors")?.text;
              const hostStorage = bii.attrs?.host_storage;
              console.log(`[CekBio RAW] ${phone} biz_identity_info: actors=${actual} host=${hostStorage}`);
              if (hostStorage === "on_premise" || actual === "self") {
                // Standard SMB — nothing special
              } else if (hostStorage === "meta" || actual === "biz_partner") {
                result.tier = result.tier || "eklusif";
              }
            }

            // catalog_status → catalog count fallback
            if (!result.catalogCount) {
              const cs = kids.catalog_status;
              if (cs) {
                const n = Number(cs.attrs?.count || cs.attrs?.value || 0);
                if (n > 0) result.catalogCount = n;
              }
            }

            // verified_level from raw
            const vlNode = kids.verified_level;
            if (vlNode) {
              const level = (vlNode.text || "").toLowerCase() || vlNode.attrs?.value || vlNode.attrs?.level;
              console.log(`[CekBio RAW] ${phone} verified_level:`, level);
              if (level === "high" || Number(level) >= 3) { result.tier = "suite"; result.isSuite = true; }
              else if (level === "eklusif" || Number(level) >= 2) { result.tier = "eklusif"; }
            }
          }

          // Fallback tier detection from Baileys biz
          if (!result.tier && biz) {
            const vName = biz.verifiedName || biz.vname || biz.verified_name;
            const vBiz = biz.isVerifiedBusiness || biz.is_verified_business || biz.verified;
            const vLevel = Number(biz.verifiedLevel || biz.verified_level || 0);
            if (vName || vBiz === true || vLevel >= 2) result.tier = "eklusif";
          }
          if (!result.tier) result.tier = "low";
          if (!result.cover && biz) result.cover = biz.cover_photo || biz.coverPhoto || biz.cover_id || biz.coverId || null;
        }

        // Usync query for deeper business detection
        if (result.exists) {
          try {
            const usyncRes = await sock.query({
              tag: "iq", attrs: { to: "s.whatsapp.net", type: "get", xmlns: "usync" },
              content: [{ tag: "usync", attrs: { sid: `${Date.now()}`, mode: "query", last: "true", index: "0", context: "interactive" },
                content: [
                  { tag: "query", attrs: {}, content: [{ tag: "business", attrs: {}, content: [{ tag: "verified_name", attrs: {} }] }, { tag: "contact", attrs: {} }] },
                  { tag: "list", attrs: {}, content: [{ tag: "user", attrs: { jid } }] }
                ]
              }]
            });
            if (usyncRes) {
              const usyncContent = usyncRes?.content?.[0]?.content || [];
              const listNode = usyncContent.find(n => n?.tag === "list");
              const userNode = listNode?.content?.find(n => n?.tag === "user");
              const bizNode = userNode?.content?.find(n => n?.tag === "business");
              const vnNode = bizNode?.content?.find(n => n?.tag === "verified_name");
              if (bizNode) {
                result.isBusiness = true;
                result.type = "wa_bisnis";
              }
              if (vnNode) {
                const vLevel = vnNode.attrs?.verified_level || "unknown";
                const hostStorage = vnNode.attrs?.host_storage;
                console.log(`[CekBio DEBUG] ${phone} vnNode.attrs:`, JSON.stringify(vnNode.attrs));
                if (vLevel === "high") { result.tier = "eklusif"; }
                else if (vLevel === "medium") { result.tier = "suite"; }
                else if (vLevel === "low") { result.tier = "standart"; }
                else { result.tier = "low"; }

                // host_storage indicates Cloud API = suite flag
                if (hostStorage) { result.isSuite = true; }

                // Extract bizSince from privacy_mode_ts
                const privTs = vnNode.attrs?.privacy_mode_ts;
                if (privTs) {
                  const d = new Date(Number(privTs) * 1000);
                  const months = ["January","February","March","April","May","June","July","August","September","October","November","December"];
                  result.bizSince = `Joined in ${months[d.getMonth()]}, ${d.getFullYear()}`;
                }

                // Parse protobuf certificate for business name
                const certBuf = vnNode.content;
                if (certBuf) {
                  const certData = parseVerifiedCert(certBuf);
                  console.log(`[CekBio DEBUG] ${phone} certData:`, JSON.stringify(certData));
                  if (certData.name) result.name = certData.name;
                  if (certData.bizType) {
                    // Suite detection from bizType (don't use as category)
                    const bt = String(certData.bizType).toUpperCase();
                    console.log(`[CekBio DEBUG] ${phone} bizType="${certData.bizType}" bt="${bt}"`);
                    if (bt.includes("ENT") || bt.includes("ENTERPRISE") || bt.includes("SUITE") || bt.includes("LARGE")) {
                      result.tier = "suite";
                      result.isSuite = true;
                    }
                  }
                }
              }
              // Contact name from usync (fallback for non-business)
              const contactNode = userNode?.content?.find(n => n?.tag === "contact");
              if (contactNode?.content) {
                const nameVal = Buffer.isBuffer(contactNode.content) ? contactNode.content.toString() : (typeof contactNode.content === "string" ? contactNode.content : null);
                if (nameVal && !result.name) result.name = nameVal;
              }
            }
          } catch {}
        }

        // AI Agent detection: query bot/automation nodes
        if (result.exists && result.isBusiness) {
          try {
            const botRes = await sock.query({
              tag: "iq", attrs: { to: "s.whatsapp.net", type: "get", xmlns: "usync" },
              content: [{ tag: "usync", attrs: { sid: `${Date.now()}`, mode: "query", last: "true", index: "0", context: "interactive" },
                content: [
                  { tag: "query", attrs: {}, content: [
                    { tag: "business", attrs: {}, content: [
                      { tag: "verified_name", attrs: {} },
                      { tag: "profile", attrs: { v: "4" } },
                    ]},
                    { tag: "bot", attrs: {} },
                    { tag: "disappearing_mode", attrs: {} },
                    { tag: "status", attrs: {} },
                  ]},
                  { tag: "list", attrs: {}, content: [{ tag: "user", attrs: { jid } }] }
                ]
              }]
            });
            if (botRes) {
              const bContent = botRes?.content?.[0]?.content || [];
              const listN = bContent.find(n => n?.tag === "list");
              const userN = listN?.content?.find(n => n?.tag === "user");
              if (userN?.content) {
                // Check for bot node (AI agent indicator)
                const botNode = userN.content.find(n => n?.tag === "bot");
                if (botNode) {
                  result.hasAiAgent = true;
                  const botAttrs = botNode.attrs || {};
                  console.log(`[CekBio AI] ${phone} botNode.attrs:`, JSON.stringify(botAttrs));
                  result.aiAgentInfo = {
                    is_bot: true,
                    bot_type: botAttrs.bot_type || botAttrs.type || "meta_ai",
                    capability: botAttrs.capability || botAttrs.capabilities || null,
                    enabled: botAttrs.enabled !== "false" && botAttrs.enabled !== "0",
                  };
                }

                // Check business node for automation/bot flags
                const bizN2 = userN.content.find(n => n?.tag === "business");
                if (bizN2) {
                  const profNode = bizN2.content?.find(n => n?.tag === "profile");
                  if (profNode?.attrs) {
                    const pa = profNode.attrs;
                    console.log(`[CekBio AI] ${phone} biz profile attrs:`, JSON.stringify(pa));

                    // Fallback bizSince from profile tag (only if it's a valid timestamp in reasonable range)
                    if (!result.bizSince && pa.tag) {
                      const tagTs = Number(pa.tag);
                      const nowSec = Math.floor(Date.now() / 1000);
                      // Valid range: 2016-01-01 (WA Business launch) to +1 day for clock skew
                      if (tagTs >= 1451606400 && tagTs <= (nowSec + 86400)) {
                        const d = new Date(tagTs * 1000);
                        if (!isNaN(d.getTime())) {
                          const months = ["January","February","March","April","May","June","July","August","September","October","November","December"];
                          result.bizSince = `Joined in ${months[d.getMonth()]}, ${d.getFullYear()}`;
                          console.log(`[CekBio DEBUG] ${phone} bizSince from tag: ${result.bizSince}`);
                        }
                      }
                    }

                    // Check automation-related fields
                    if (pa.is_ai_enabled === "true" || pa.ai_agent === "true" ||
                        pa.automation === "true" || pa.bot_enabled === "true" ||
                        pa.has_ai === "true" || pa.ai_enabled === "true") {
                      result.hasAiAgent = true;
                      if (!result.aiAgentInfo) {
                        result.aiAgentInfo = { is_bot: true, bot_type: "business_ai", capability: null, enabled: true };
                      }
                    }
                  }
                  // Check verified_name for automation indicator
                  const vnN2 = bizN2.content?.find(n => n?.tag === "verified_name");
                  if (vnN2?.attrs) {
                    const vna = vnN2.attrs;
                    if (vna.automation || vna.bot || vna.ai_enabled || vna.is_bot) {
                      result.hasAiAgent = true;
                      if (!result.aiAgentInfo) {
                        result.aiAgentInfo = { is_bot: true, bot_type: "verified_ai", capability: null, enabled: true };
                      }
                    }
                  }
                }

                // Log full user node for debugging
                console.log(`[CekBio AI] ${phone} full userNode tags:`,
                  (userN.content || []).map(n => `${n?.tag}[${JSON.stringify(n?.attrs || {})}]`).join(", "));
              }
            }
          } catch (e) {
            console.log(`[CekBio AI] ${phone} bot query error:`, e.message?.slice(0, 60));
          }

          // Additional: check getBusinessProfile response for automation fields
          try {
            const fullBiz = await sock.getBusinessProfile(jid);
            if (fullBiz) {
              console.log(`[CekBio AI] ${phone} fullBiz keys:`, Object.keys(fullBiz).join(", "));
              // Check common automation/AI fields
              if (fullBiz.ai_enabled || fullBiz.is_ai || fullBiz.automation ||
                  fullBiz.bot_info || fullBiz.ai_agent || fullBiz.chatbot ||
                  fullBiz.has_automation || fullBiz.automated_messages) {
                result.hasAiAgent = true;
                if (!result.aiAgentInfo) {
                  result.aiAgentInfo = {
                    is_bot: true,
                    bot_type: fullBiz.bot_info?.type || "business_automation",
                    capability: fullBiz.bot_info?.capability || fullBiz.automation?.type || null,
                    enabled: true,
                  };
                }
              }
            }
          } catch {}
        }

        // Get catalog count for business accounts
        if (result.isBusiness && !result.catalogCount) {
          try {
            if (typeof sock.getCatalog === "function") {
              const catalog = await sock.getCatalog({ jid, limit: 10 });
              const products = catalog?.products || catalog?.data || [];
              if (products.length > 0) result.catalogCount = products.length;
            }
          } catch {}
        }

        if (!result.type) result.type = "wa_biasa";
      } catch (e) { /* skip */ }
      job.results.push(result);
      job.progress++;
    }

    // Process in batches
    for (let i = 0; i < jids.length && job.running; i += batch) {
      const chunk = jids.slice(i, i + batch);
      const phoneChunk = phones.slice(i, i + batch);
      await Promise.allSettled(
        chunk.map((jid, idx) => checkOne(jid, phoneChunk[idx]))
      );
      if (i + batch < jids.length && job.running) await sleep(1000);
    }

    // Auto re-check: business accounts with incomplete data get ONE retry.
    // A single WA server hiccup can drop the biz profile IQ silently.
    const needRetry = job.results.filter(r =>
      r.exists && r.isBusiness && (!r.bizSince || r.bizSince === "-" || !r.name)
    );
    if (needRetry.length > 0 && job.running) {
      console.log(`[${uid}] CekBio retry: ${needRetry.length} incomplete business results`);
      job.retryPhase = true;
      const retryBatch = Math.min(batch, 30); // safer rate on retry
      for (let i = 0; i < needRetry.length && job.running; i += retryBatch) {
        const chunk = needRetry.slice(i, i + retryBatch);
        await Promise.allSettled(chunk.map(async (old) => {
          try {
            // Remove old result and re-run checkOne
            const idx = job.results.indexOf(old);
            if (idx >= 0) job.results.splice(idx, 1);
            job.progress = Math.max(0, job.progress - 1);
            const jid = `${old.number.replace(/^\+/, "")}@s.whatsapp.net`;
            await checkOne(jid, old.number);
          } catch {}
        }));
        if (i + retryBatch < needRetry.length && job.running) await sleep(1500);
      }
      job.retryPhase = false;
    }

    job.running = false;
    console.log(`[${uid}] CekBio done: ${job.results.length}/${job.total} (retried: ${needRetry.length})`);
  })();

  return { status: "started", total: jids.length, batch };
}

// ═══ HTTP SERVER ═══
function parseBody(req) {
  return new Promise((resolve, reject) => {
    let data = "";
    req.on("data", chunk => { data += chunk; });
    req.on("end", () => {
      try { resolve(JSON.parse(data || "{}")); }
      catch { resolve({}); }
    });
    req.on("error", reject);
  });
}

function sendJson(res, status, data) {
  res.writeHead(status, { "Content-Type": "application/json" });
  res.end(JSON.stringify(data));
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://localhost:${PORT}`);
  const parts = url.pathname.split("/").filter(Boolean);

  try {
    // POST /pair  — body: {userId, phone, addAccount?}
    if (req.method === "POST" && parts[0] === "pair") {
      const body = await parseBody(req);
      const { userId, phone, addAccount } = body;
      if (!userId || !phone) return sendJson(res, 400, { error: "missing userId or phone" });

      const asBackup = !!addAccount;
      const result = await createSocket(userId, phone, { asBackup });
      return sendJson(res, 200, {
        code: result?.pairCode ?? null,
        status: "pairing",
        sessionKey: result?.sessionKey || String(userId),
        backup: asBackup,
      });
    }

    // GET /sessions — list all active WA sessions
    if (req.method === "GET" && parts[0] === "sessions") {
      const all = [];
      for (const [uid, entry] of sockets.entries()) {
        all.push({
          uid,
          ownerId: entry.ownerId || ownerOfKey(uid),
          connected: entry.connected,
          waNumber: entry.waNumber || null,
        });
      }
      return sendJson(res, 200, { sessions: all });
    }

    // GET /accounts/:uid — list akun WA (utama + cadangan)
    if (req.method === "GET" && parts[0] === "accounts" && parts[1]) {
      const owner = String(parts[1]);
      const active = getActiveKey(owner);
      const accounts = listAccountKeys(owner).map((key) => {
        const entry = sockets.get(key);
        return {
          key,
          active: key === active,
          connected: !!entry?.connected,
          waNumber: entry?.waNumber || null,
          exists: sessionExistsOnDisk(key) || !!entry,
        };
      });
      return sendJson(res, 200, { accounts, active });
    }

    // POST /switch/:uid — ganti akun aktif (body: {accountKey})
    if (req.method === "POST" && parts[0] === "switch" && parts[1]) {
      const owner = String(parts[1]);
      const body = await parseBody(req);
      const accountKey = String(body.accountKey || "");
      if (!accountKey || !isKeyForUser(accountKey, owner)) {
        return sendJson(res, 400, { error: "invalid accountKey" });
      }
      if (!sockets.has(accountKey) && !sessionExistsOnDisk(accountKey)) {
        return sendJson(res, 404, { error: "account_not_found" });
      }
      if (!sockets.has(accountKey) && sessionExistsOnDisk(accountKey)) {
        try { await reconnectSocket(accountKey); } catch {}
      }
      setActiveKey(owner, accountKey);
      const entry = sockets.get(accountKey);
      return sendJson(res, 200, {
        status: "switched",
        active: accountKey,
        connected: !!entry?.connected,
        waNumber: entry?.waNumber || null,
      });
    }

    // GET /status/:uid
    if (req.method === "GET" && parts[0] === "status" && parts[1]) {
      const owner = String(parts[1]);
      const { key, entry } = resolveEntry(owner);
      const accounts = listAccountKeys(owner);
      if (!entry) {
        return sendJson(res, 200, {
          connected: false,
          exists: sessionExistsOnDisk(key) || accounts.length > 0,
          accountsCount: accounts.length,
          activeKey: key,
        });
      }
      return sendJson(res, 200, {
        connected: entry.connected,
        exists: true,
        waNumber: entry.waNumber,
        pairCode: entry.pairCode,
        accountsCount: accounts.length,
        activeKey: key,
      });
    }

    // GET /groups/:uid
    if (req.method === "GET" && parts[0] === "groups" && parts[1]) {
      const { key: uid, entry } = resolveEntry(parts[1]);
      if (!entry?.connected) return sendJson(res, 200, { error: "not_connected", groups: [] });

      const sock = entry.sock;
      const groups = await sock.groupFetchAllParticipating();
      const groupList = Object.values(groups);

      // Multiple JID detection methods
      const myJid = sock.user?.id || "";
      const myNum = myJid.split(":")[0].split("@")[0];
      const myFullJid = myJid; // e.g. "628xxx:123@s.whatsapp.net"
      const myLid = sock.user?.lid || ""; // LID if available
      const myLidNum = myLid ? myLid.split(":")[0].split("@")[0] : "";

      console.log(`[${uid}] Group detection - JID: ${myJid}, Num: ${myNum}, LID: ${myLid}`);

      const results = groupList.map(g => {
        // Try multiple methods to find ourselves in participants
        let me = null;
        let matchMethod = "none";

        for (const p of (g.participants || [])) {
          const pId = p.id || "";
          const pNum = pId.split("@")[0].split(":")[0];

          // Method 1: exact phone number match
          if (pNum === myNum) {
            me = p;
            matchMethod = "phone";
            break;
          }
          // Method 2: full JID contains our number
          if (myNum && pId.includes(myNum)) {
            me = p;
            matchMethod = "partial_jid";
            break;
          }
          // Method 3: LID match
          if (myLidNum && pNum === myLidNum) {
            me = p;
            matchMethod = "lid";
            break;
          }
          // Method 4: LID in participant ID
          if (myLid && pId.includes(myLid.split("@")[0])) {
            me = p;
            matchMethod = "lid_partial";
            break;
          }
        }

        const isAdmin = me?.admin === "admin" || me?.admin === "superadmin";
        const isSuperAdmin = me?.admin === "superadmin";

        return {
          name: g.subject || "Tanpa Nama",
          id: g.id,
          members: g.participants?.length || 0,
          isAdmin,
          isSuperAdmin,
          matchMethod,
          participants: g.participants?.map(p => ({
            id: p.id,
            admin: p.admin || null,
            isLid: p.id?.includes("lid") || false,
          })) || [],
        };
      });

      // Return ALL groups where user is admin
      const adminGroups = results.filter(g => g.isAdmin);
      
      // If no admin found with strict matching, also return groups where user is member
      // (fallback: return all groups and let user see them)
      if (adminGroups.length === 0) {
        console.log(`[${uid}] No admin groups found with strict matching, returning all groups`);
        return sendJson(res, 200, { groups: results, total: results.length, fallback: true });
      }
      
      return sendJson(res, 200, { groups: adminGroups, total: results.length });
    }

    // POST /citer/:uid/:gid
    if (req.method === "POST" && parts[0] === "citer" && parts[1] && parts[2]) {
      const uid = parts[1];
      const gid = decodeURIComponent(parts[2]);
      const result = await citerGb(uid, gid);
      return sendJson(res, 200, result);
    }

    // GET /citer-status/:uid/:gid
    if (req.method === "GET" && parts[0] === "citer-status" && parts[1] && parts[2]) {
      const jobKey = `${parts[1]}_${decodeURIComponent(parts[2])}`;
      const job = citerJobs.get(jobKey);
      if (!job) return sendJson(res, 200, { exists: false });
      return sendJson(res, 200, { progress: job.progress, total: job.total, running: job.running, error: job.error });
    }

    // POST /addmember/:uid/:gid
    if (req.method === "POST" && parts[0] === "addmember" && parts[1] && parts[2]) {
      const uid = parts[1];
      const gid = decodeURIComponent(parts[2]);
      const body = await parseBody(req);
      const { phones } = body;
      if (!phones || !Array.isArray(phones) || phones.length === 0) {
        return sendJson(res, 400, { error: "missing phones array" });
      }
      const result = await addMembers(uid, gid, phones);
      return sendJson(res, 200, result);
    }

    // GET /addmember-status/:uid/:gid
    if (req.method === "GET" && parts[0] === "addmember-status" && parts[1] && parts[2]) {
      const jobKey = `${parts[1]}_${decodeURIComponent(parts[2])}`;
      const job = addJobs.get(jobKey);
      if (!job) return sendJson(res, 200, { exists: false });
      return sendJson(res, 200, {
        added: job.added, failed: job.failed, total: job.total,
        running: job.running, errors: job.errors.slice(-20), skipped: job.skipped,
      });
    }

    // POST /disconnect/:uid — logout + hapus session dari disk (tidak auto-reconnect)
    // Optional body: { accountKey } untuk disconnect akun spesifik; default = akun aktif
    if (req.method === "POST" && parts[0] === "disconnect" && parts[1]) {
      const owner = String(parts[1]);
      let body = {};
      try { body = await parseBody(req); } catch {}
      let targetKey = body.accountKey ? String(body.accountKey) : getActiveKey(owner);
      if (body.accountKey && !isKeyForUser(targetKey, owner)) {
        return sendJson(res, 400, { error: "invalid accountKey" });
      }
      if (!sockets.has(targetKey) && !sessionExistsOnDisk(targetKey)) {
        // sudah bersih
        const remaining = listAccountKeys(owner);
        return sendJson(res, 200, {
          status: "disconnected",
          wiped: targetKey,
          remaining,
          active: remaining[0] || null,
        });
      }
      const result = await wipeSession(targetKey, { logout: true });
      const remaining = result.remaining || [];
      let activeEntry = null;
      if (remaining.length) {
        const activeKey = getActiveKey(owner);
        activeEntry = sockets.get(activeKey) || null;
      }
      return sendJson(res, 200, {
        status: "disconnected",
        wiped: result.wiped,
        remaining,
        active: remaining.length ? getActiveKey(owner) : null,
        waNumber: activeEntry?.waNumber || null,
        connected: !!activeEntry?.connected,
      });
    }

    // POST /creategroup/:uid
    if (req.method === "POST" && parts[0] === "creategroup" && parts[1]) {
      const uid = parts[1];
      const body = await parseBody(req);
      const { name, phones } = body;
      if (!name || !phones || !Array.isArray(phones)) {
        return sendJson(res, 400, { error: "missing name or phones" });
      }
      const result = await createGroup(uid, name, phones);
      return sendJson(res, 200, result);
    }

    // POST /stop-citer/:uid/:gid
    if (req.method === "POST" && parts[0] === "stop-citer" && parts[1] && parts[2]) {
      const jobKey = `${parts[1]}_${decodeURIComponent(parts[2])}`;
      const job = citerJobs.get(jobKey);
      if (job) job.running = false;
      return sendJson(res, 200, { status: "stopped" });
    }

    // POST /stop-add/:uid/:gid
    if (req.method === "POST" && parts[0] === "stop-add" && parts[1] && parts[2]) {
      const jobKey = `${parts[1]}_${decodeURIComponent(parts[2])}`;
      const job = addJobs.get(jobKey);
      if (job) job.running = false;
      return sendJson(res, 200, { status: "stopped" });
    }

    // POST /cekbio/:uid
    if (req.method === "POST" && parts[0] === "cekbio" && parts[1]) {
      const uid = parts[1];
      const body = await parseBody(req);
      const { phones, batch } = body;
      if (!phones || !Array.isArray(phones) || phones.length === 0) {
        return sendJson(res, 400, { error: "missing phones array" });
      }
      const result = await cekBio(uid, phones, batch || 50);
      return sendJson(res, 200, result);
    }

    // GET /cekbio-status/:uid
    if (req.method === "GET" && parts[0] === "cekbio-status" && parts[1]) {
      const uid = parts[1];
      const job = cekbioJobs.get(uid);
      if (!job) return sendJson(res, 200, { exists: false });
      return sendJson(res, 200, {
        progress: job.progress, total: job.total, running: job.running,
        batch: job.batch, results: job.running ? [] : job.results,
      });
    }

    // POST /stop-cekbio/:uid
    if (req.method === "POST" && parts[0] === "stop-cekbio" && parts[1]) {
      const uid = parts[1];
      const job = cekbioJobs.get(uid);
      if (job) job.running = false;
      return sendJson(res, 200, { status: "stopped" });
    }

    sendJson(res, 404, { error: "not_found" });
  } catch (err) {
    console.error("Server error:", err);
    sendJson(res, 500, { error: err.message });
  }
});

// ═══ STARTUP ═══
async function reconnectAllSessions() {
  if (!fs.existsSync(SESSIONS_DIR)) return;
  const dirs = fs.readdirSync(SESSIONS_DIR).filter(d =>
    fs.statSync(path.join(SESSIONS_DIR, d)).isDirectory()
  );
  for (const uid of dirs) {
    const sessionDir = path.join(SESSIONS_DIR, uid);
    if (fs.readdirSync(sessionDir).length > 0) {
      console.log(`Reconnecting session: ${uid}`);
      try {
        await reconnectSocket(uid);
        await sleep(2000);
      } catch (err) {
        console.error(`Failed reconnect ${uid}:`, err.message);
      }
    }
  }
}

server.listen(PORT, async () => {
  console.log(`\n WA Server running on port ${PORT}`);
  console.log(` Sessions dir: ${SESSIONS_DIR}`);
  console.log(` Citer iterations: ${CITER_ITERATIONS}`);
  console.log(` Add batch: ${ADD_BATCH_SIZE} per wave\n`);
  await reconnectAllSessions();

  // Auto-refresh sessions every 5 minutes to keep connections alive
  setInterval(async () => {
    for (const [uid, entry] of sockets.entries()) {
      if (entry.manualClose) continue;
      if (!entry.connected && entry.sock) {
        console.log(`[Auto-refresh] Reconnecting ${uid}...`);
        try { await reconnectSocket(uid); } catch {}
      }
    }
  }, 5 * 60 * 1000);
});
