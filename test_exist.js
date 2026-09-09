// Test WhatsApp /v2/exist check
const { randomBytes, createHash } = require('crypto')
const { generateKeyPair, sign } = require('curve25519-js')

const md5 = (s) => createHash('md5').update(s).digest('hex').toUpperCase()

// fallback for missing `phone` package: use libphonenumber-js
let phone
try { phone = require('phone') } catch (e) {
  const { parsePhoneNumberFromString } = require('libphonenumber-js')
  phone = (input) => {
    const parsed = parsePhoneNumberFromString(String(input))
    if (!parsed || !parsed.isValid()) return { isValid: false }
    return { isValid: true, countryCode: '+' + parsed.countryCallingCode, phoneNumber: parsed.nationalNumber }
  }
}

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

const checkWhatsAppCode = async (number, method = 'sms') => {
  number = number.replace(/\D/g, '')
  number = phone('+' + number)
  if (!number.isValid) return number

  const identityKey = createKeyPair()
  const noiseKey = createKeyPair()
  const signedPreKey = signedKeyPair(identityKey)

  const regId = Buffer.alloc(4)
  regId.writeInt32BE(Uint16Array.from(randomBytes(2))[0] & 16383)
  const skeyId = Buffer.alloc(3)
  skeyId.writeInt16BE(1)

  const cc = number.countryCode.replace('+', '')
  const inNum = number.phoneNumber.replace(number.countryCode, '')
  const full = cc + inNum

  const params = {
    cc: cc,
    in: inNum,
    to: full,
    lg: 'en',
    lc: 'GB',
    method: method,
    mcc: '510',
    mnc: '000',
    token: md5(full + 'Pd0As4oDTQ9KOPzY'),
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
  const headers = {}
  headers['User-Agent'] = 'WhatsApp/2.26.23.74 iOS/17.5.1 Device/Apple-iPhone_13'
  const response = await fetch(
    'https://v.whatsapp.net/v2/code?' + parameter.join('&'),
    { headers }
  )
  const json = await response.json()
  return json
}

const checkWhatsApp = async (number) => {
  number = number.replace(/\D/g, '')
  number = phone('+' + number)
  if (!number.isValid) return number

  const identityKey = createKeyPair()
  const noiseKey = createKeyPair()
  const signedPreKey = signedKeyPair(identityKey)

  const regId = Buffer.alloc(4)
  regId.writeInt32BE(Uint16Array.from(randomBytes(2))[0] & 16383)
  const skeyId = Buffer.alloc(3)
  skeyId.writeInt16BE(1)

  const params = {
    cc: number.countryCode.replace('+', ''),
    in: number.phoneNumber.replace(number.countryCode, ''),
    lg: 'en',
    lc: 'GB',
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
  const headers = {}
  headers['User-Agent'] = 'WhatsApp/2.26.23.74 iOS/17.5.1 Device/Apple-iPhone_13'
  const response = await fetch(
    'https://v.whatsapp.net/v2/exist?' + parameter.join('&'),
    { headers }
  )
  const json = await response.json()
  return json
}

const NUMBER = process.argv[2] || '+6285757411154'
const MODE = process.argv[3] || 'exist'

const run = MODE === 'code'
  ? checkWhatsAppCode(NUMBER, process.argv[4] || 'sms')
  : checkWhatsApp(NUMBER)

run.then((r) => {
  console.log('=== RESULT', MODE, 'for', NUMBER, '===')
  console.log(JSON.stringify(r, null, 2))
}).catch((e) => {
  console.error('ERROR:', e.message)
  process.exit(1)
})
