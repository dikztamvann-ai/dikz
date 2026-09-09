// Test /v2/code with different token formulas
const { randomBytes, createHash } = require('crypto')
const { generateKeyPair, sign } = require('curve25519-js')

const md5 = (s) => createHash('md5').update(s).digest('hex')
const MD5 = (s) => md5(s).toUpperCase()

const createKeyPair = () => {
  const keyPair = generateKeyPair(randomBytes(32))
  return { private: Buffer.from(keyPair.private), public: Buffer.from(keyPair.public) }
}
const generateSignalPubKey = (pubKey) => pubKey.length === 33 ? pubKey : Buffer.concat([Buffer.from([5]), pubKey])
const signedKeyPair = (identityKeyPair) => {
  const preKey = createKeyPair()
  const pubKey = generateSignalPubKey(preKey.public)
  const signature = sign(identityKeyPair.private, pubKey)
  return { keyPair: preKey, signature }
}
const toBase64Url = (arg) => Buffer.from(arg).toString('base64url')
const toUrlHex = (buffer) => [...buffer].map((x) => '%' + x.toString(16).padStart(2, '0')).join('')

const CC = '62'
const IN = '85757411154'
const FULL = CC + IN

// historical token salts from old registration tools
const TOKEN_FORMULAS = {
  'android_md5_salt': () => MD5(FULL + 'Pd0As4oDTQ9KOPzY'),
  'android_md5_salt_lower': () => md5(FULL + 'Pd0As4oDTQ9KOPzY'),
  'ios_md5_salt': () => md5('6\\IrNc220nYJG' + CC + IN),
  'random_md5': () => md5(randomBytes(16).toString('hex')),
}

const tryCode = async (tokenName, token) => {
  const identityKey = createKeyPair()
  const noiseKey = createKeyPair()
  const signedPreKey = signedKeyPair(identityKey)
  const regId = Buffer.alloc(4)
  regId.writeInt32BE(Uint16Array.from(randomBytes(2))[0] & 16383)
  const skeyId = Buffer.alloc(3)
  skeyId.writeInt16BE(1)

  const params = {
    cc: CC, in: IN, to: FULL, lg: 'en', lc: 'GB',
    method: 'sms', mcc: '510', mnc: '000',
    token: token,
    authkey: toBase64Url(noiseKey.public),
    e_regid: toBase64Url(regId),
    e_keytype: 'BQ',
    e_ident: toBase64Url(identityKey.public),
    e_skey_id: toBase64Url(skeyId),
    e_skey_val: toBase64Url(signedPreKey.keyPair.public),
    e_skey_sig: toBase64Url(signedPreKey.signature),
    id: toUrlHex(randomBytes(20))
  }
  const parameter = []
  for (const param in params) parameter.push(param + '=' + params[param])
  const headers = { 'User-Agent': 'WhatsApp/2.26.23.74 iOS/17.5.1 Device/Apple-iPhone_13' }
  const response = await fetch('https://v.whatsapp.net/v2/code?' + parameter.join('&'), { headers })
  const json = await response.json()
  console.log(`--- token formula: ${tokenName}`)
  console.log(JSON.stringify(json, null, 2))
  return json
}

;(async () => {
  for (const [name, fn] of Object.entries(TOKEN_FORMULAS)) {
    try {
      const r = await tryCode(name, fn())
      if (r.reason !== 'missing_param' && r.reason !== 'bad_token') {
        console.log('>>> ACCEPTED with', name)
        break
      }
    } catch (e) {
      console.error(name, 'ERROR:', e.message)
    }
    await new Promise((r) => setTimeout(r, 1500))
  }
})()
