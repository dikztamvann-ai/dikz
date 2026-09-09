// Cross-check JS side: fixed seed -> public + XEdDSA signature
const { generateKeyPair, sign } = require('curve25519-js')

const seed = Buffer.from('00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff', 'hex')
const seed2 = Buffer.from('deadbeefcafebabe0123456789abcdefdeadbeefcafebabe0123456789abcdef', 'hex')

const kp = generateKeyPair(seed)
const kp2 = generateKeyPair(seed2)
const priv = Buffer.from(kp.private)
const pub = Buffer.from(kp.public)
const pub2 = Buffer.from(kp2.public)

// msg = 0x05-prefixed prekey public (33 bytes)
const msg = Buffer.concat([Buffer.from([5]), pub2])
const sig = sign(priv, msg)

console.log(JSON.stringify({
  pub: pub.toString('hex'),
  msg: msg.toString('hex'),
  sig: Buffer.from(sig).toString('hex')
}))
