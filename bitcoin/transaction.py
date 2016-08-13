#!/usr/bin/python
import binascii, re, json, sys, binascii
from bitcoin.main import *
from _functools import reduce
from bitcoin.pyspecials import *
from bitcoin.bci import fetchtx


# Transaction serialization and deserialization

def deserialize(tx):
    if isinstance(tx, str) and RE_HEX_CHARS.match(tx):
        return json_hexlify(deserialize(binascii.unhexlify(tx)))
    # http://stackoverflow.com/questions/4851463/python-closure-write-to-variable-in-parent-scope
    # Python's scoping rules are demented, requiring me to make pos an object
    # so that it is call-by-reference
    pos = [0]

    def read_as_int(bytez):
        pos[0] += bytez
        return decode(tx[pos[0]-bytez:pos[0]][::-1], 256)

    def read_var_int():
        pos[0] += 1
        
        val = from_byte_to_int(tx[pos[0]-1])
        if val < 253:
            return val
        return read_as_int(pow(2, val - 252))

    def read_bytes(bytez):
        pos[0] += bytez
        return tx[pos[0]-bytez:pos[0]]

    def read_var_string():
        size = read_var_int()
        return read_bytes(size)

    obj = {"ins": [], "outs": []}
    obj["version"] = read_as_int(4)
    ins = read_var_int()
    for i in range(ins):
        obj["ins"].append(
        {
            "outpoint": {
                "hash": read_bytes(32)[::-1],
                "index": read_as_int(4)
            },
            "script": read_var_string(),
            "sequence": read_as_int(4)
        })
    outs = read_var_int()
    for i in range(outs):
        obj["outs"].append(
        {
            "value": read_as_int(8),
            "script": read_var_string()
        })
    obj["locktime"] = read_as_int(4)
    return obj
        

def serialize(txobj):
    #if isinstance(txobj, bytes):
    #    txobj = bytes_to_hex_string(txobj)
    o = []
    if json_is_base(txobj, 16):
        json_changedbase = json_unhexlify(txobj)
        hexlified = safe_hexlify(serialize(json_changedbase))
        return hexlified
    o.append(encode(txobj["version"], 256, 4)[::-1])
    o.append(num_to_var_int(len(txobj["ins"])))
    for inp in txobj["ins"]:
        o.append(inp["outpoint"]["hash"][::-1])
        o.append(encode(inp["outpoint"]["index"], 256, 4)[::-1])
        o.append(num_to_var_int(len(inp["script"]))+(inp["script"] if inp["script"] or is_python2 else bytes()))
        o.append(encode(inp["sequence"], 256, 4)[::-1])
    o.append(num_to_var_int(len(txobj["outs"])))
    for out in txobj["outs"]:
        o.append(encode(out["value"], 256, 8)[::-1])
        o.append(num_to_var_int(len(out["script"]))+out["script"])
    o.append(encode(txobj["locktime"], 256, 4)[::-1])

    return ''.join(o) if is_python2 else reduce(lambda x,y: x+y, o, bytes())

# Hashing transactions for signing

SIGHASH_ALL = 1
SIGHASH_NONE = 2
SIGHASH_SINGLE = 3
# this works like SIGHASH_ANYONECANPAY | SIGHASH_ALL, might as well make it explicit while
# we fix the constant
SIGHASH_ACP = 0x80
SIGHASH_ANYONECANPAY = SIGHASH_ACP | SIGHASH_ALL


def signature_form(tx, i, script, hashcode=SIGHASH_ALL):
    import copy
    i, hashcode = int(i), int(hashcode)
    if isinstance(tx, string_or_bytes_types):
        return serialize(signature_form(deserialize(tx), i, script, hashcode))
    newtx = copy.deepcopy(tx)
    for inp in newtx["ins"]:
        inp["script"] = ""
    newtx["ins"][i]["script"] = script
    if hashcode == SIGHASH_NONE:
        newtx["outs"] = []
    elif hashcode == SIGHASH_SINGLE:
        num_ins = len(newtx["ins"])
        newtx["outs"] = newtx["outs"][:num_ins]
        for out in newtx["outs"][:num_ins - 1]:     # del outs @ lower index
            out['value'] = 2**64 - 1
            out['script'] = ""
    elif hashcode == SIGHASH_ANYONECANPAY:
        newtx["ins"] = [newtx["ins"][i]]
    else:
        pass
    return newtx


# Making the actual signatures

def der_encode_sig(*args):
    """Takes ([vbyte], r, s) as ints and returns hex der encode sig"""
    if len(args) == 3:
        v,r,s = args
    elif len(args) == 2:
        r,s = args
    elif len(args) == 1 and isinstance(args[0], tuple):
        return der_encode_sig(*args[0])
    b1, b2 = encode(r, 256), encode(s, 256)
    if len(b1) and changebase(b1[0], 256, 16, 1) in "89abcdef":	# add null bytes if interpreted as negative number
        b1 = b'\x00' + b1
    if len(b2) and ord(b2[0]) & 0x80:
        b2 = b'\x00' + b2
    left  = b'\x02' + encode(len(b1), 256, 1) + b1
    right = b'\x02' + encode(len(b2), 256, 1) + b2
    sighex = safe_hexlify(b'\x30' + encode(len(left+right), 256, 1) + left + right)
    #assert is_bip66(sighex)
    return sighex


def der_decode_sig(sig):
    """Takes DER sig (incl. hashcode), returns v,r,s as ints"""
    leftlen = decode(sig[6:8], 16)*2
    left = sig[8:8+leftlen]
    rightlen = decode(sig[10+leftlen:12+leftlen], 16)*2
    right = sig[12+leftlen:12+leftlen+rightlen]
    #assert 3*2 + leftlen + 3*2 + rightlen + 1*2 == len(sig) 	
    return (None, decode(left, 16), decode(right, 16))
    

def is_bip66(sig):
    """Checks hex DER sig for BIP66 compliance"""
    #https://raw.githubusercontent.com/bitcoin/bips/master/bip-0066.mediawiki
    #0x30  [total-len]  0x02  [R-len]  [R]  0x02  [S-len]  [S]  [sighash]
    sig = bytearray.fromhex(sig) if (isinstance(sig, string_types) and
             RE_HEX_CHARS.match(sig)) else bytearray(sig)
    if sig[1] == len(sig)-2: 
        sig.extend(b"\1")		# add SIGHASH for BIP66 check

    if len(sig) < 9 or len(sig) > 73: return False
    if (sig[0] != 0x30): return False
    if (sig[1] != len(sig)-3): return False
    rlen = sig[3]
    if (5+rlen >= len(sig)): return False
    slen = sig[5+rlen]
    if (rlen + slen + 7 != len(sig)): return False
    if (sig[2] != 0x02): return False
    if (rlen == 0): return False
    if (sig[4] & 0x80): return False
    if (rlen > 1 and (sig[4] == 0) and not (sig[5] & 0x80)): return False
    if (sig[4+rlen] != 0x02): return False
    if (slen == 0): return False
    if (sig[rlen+6] & 0x80): return False
    if (slen > 1 and (sig[6+rlen] == 0) and not (sig[7+rlen] & 0x80)): return False
    
    return True


def txhash(tx, hashcode=None):
    if isinstance(tx, string_types) and RE_HEX_CHARS.match(tx):
        tx = changebase(tx, 16, 256)
    if hashcode is not None:
        return dbl_sha256(from_str_to_bytes(tx) + from_int_to_le_bytes(int(hashcode), 4))
    else:
        return safe_hexlify(bin_dbl_sha256(tx)[::-1])


def bin_txhash(tx, hashcode=None):
    return binascii.unhexlify(txhash(tx, hashcode))


def ecdsa_tx_sign(tx, priv, hashcode=SIGHASH_ALL):
    """Returns DER sig for rawtx w/ hashcode appended"""
    rawsig = ecdsa_raw_sign(bin_txhash(tx, hashcode), priv)
    return der_encode_sig(*rawsig) + encode(hashcode, 16, 2)


def ecdsa_tx_verify(tx, sig, pub, hashcode=SIGHASH_ALL):
    return ecdsa_raw_verify(bin_txhash(tx, hashcode), der_decode_sig(sig), pub)


def ecdsa_tx_recover(tx, sig, hashcode=SIGHASH_ALL):
    """Recover valid pubkey(s) for signed tx"""
    z = bin_txhash(tx, hashcode)
    _, r, s = der_decode_sig(sig)
    left = ecdsa_raw_recover(z, (0, r, s))
    right = ecdsa_raw_recover(z, (1, r, s))
    return encode_pubkey(left, 'hex'), encode_pubkey(right, 'hex')

# Scripts

def mk_pubkey_script(addr):
    # Keep the auxiliary functions around for altcoins' sake
    return '76a914' + b58check_to_hex(addr) + '88ac'


def mk_scripthash_script(addr):
    return 'a914' + b58check_to_hex(addr) + '87'


# Address representation to output script

def address_to_script(addr):
    if addr[0] == '3' or addr[0] == '2':
        return mk_scripthash_script(addr)
    else:
        return mk_pubkey_script(addr)

# Output script to address representation

def script_to_address(script, vbyte=0):
    if RE_HEX_CHARS.match(script):
        script = binascii.unhexlify(script)
    if script[:3] == b'\x76\xa9\x14' and script[-2:] == b'\x88\xac' and len(script) == 25:
        return bin_to_b58check(script[3:-2], vbyte)  # pubkey hash addresses
    else:
        if vbyte in [111, 196]:     # Testnet
            scripthash_byte = 196
        else:
            scripthash_byte = 0x05 if not vbyte else vbyte
        return bin_to_b58check(script[2:-1], scripthash_byte)   # BIP0016 scripthash addresses


def p2sh_scriptaddr(script, magicbyte=5):
    if RE_HEX_CHARS.match(script):
        script = binascii.unhexlify(script)
    return hex_to_b58check(hash160(script), magicbyte)

scriptaddr = p2sh_scriptaddr


def deserialize_script(script):
    if isinstance(script, str) and RE_HEX_CHARS.match(script):
       return json_hexlify(deserialize_script(safe_unhexlify(script)))
    out, pos = [], 0
    while pos < len(script):
        code = from_byte_to_int(script[pos])
        if code == 0:
            out.append(None)
            pos += 1
        elif code <= 75:
            out.append(script[pos+1:pos+1+code])
            pos += 1 + code
        elif code <= 78:
            szsz = pow(2, code - 76)
            sz = decode(script[pos+szsz: pos:-1], 256)
            out.append(script[pos + 1 + szsz:pos + 1 + szsz + sz])
            pos += 1 + szsz + sz
        elif code <= 96:
            out.append(code - 80)
            pos += 1
        else:
            out.append(code)
            pos += 1
    return out


def serialize_script_unit(unit):
    if isinstance(unit, int):
        if unit < 16:
            return from_int_to_byte(unit + 80)
        else:
            return from_int_to_byte(unit)
    elif unit is None:
        return b'\x00'
    else:
        if len(unit) <= 75:
            return from_int_to_byte(len(unit))+unit
        elif len(unit) < 256:
            return from_int_to_byte(76)+from_int_to_byte(len(unit))+unit
        elif len(unit) < 65536:
            return from_int_to_byte(77)+encode(len(unit), 256, 2)[::-1]+unit
        else:
            return from_int_to_byte(78)+encode(len(unit), 256, 4)[::-1]+unit


if is_python2:
    def serialize_script(script):
        if json_is_base(script, 16):
            return binascii.hexlify(serialize_script(json_changebase(script,
                                    lambda x: binascii.unhexlify(x))))
        return ''.join(map(serialize_script_unit, script))
else:
    def serialize_script(script):
        if json_is_base(script, 16):
            script_bin = json_unhexlify(script)
            return safe_hexlify(serialize_script(script_bin))
        else:
            result = bytes()
            for b in map(serialize_script_unit, script):
                result += b if isinstance(b, bytes) else bytes(b, 'utf-8')
            return result


def mk_multisig_script(*args):  
    # [pubs],k or pub1,pub2...pub[n],k
    if isinstance(args[0], list):
        pubs, k = args[0], int(args[1])
    else:
        pubs = list(filter(lambda x: len(str(x)) >= 32, args))
        k = int(args[len(pubs)])
    return serialize_script([k] + pubs + [len(pubs)] + [0xae])

# Signing and verifying

def verify_tx_input(tx, i, script, sig, pub):
    """tx = scriptsig replaced by scriptPubKey"""
    if RE_HEX_CHARS.match(tx):
        tx = binascii.unhexlify(tx)
    if RE_HEX_CHARS.match(script):
        script = binascii.unhexlify(script)
    if not (RE_DER.match(sig) or RE_HEX_CHARS.match(sig)):
        sig = safe_hexlify(sig)
    hashcode = decode(sig[-2:], 16)
    modtx = signature_form(tx, int(i), script, hashcode)
    return ecdsa_tx_verify(modtx, sig, pub, hashcode)


def sign(tx, i, priv, hashcode=SIGHASH_ALL):
    i = int(i)
    if (not is_python2 and isinstance(re, bytes)) or not RE_HEX_CHARS.match(tx):
        return binascii.unhexlify(sign(safe_hexlify(tx), i, priv))
    if len(priv) <= 33:
        priv = safe_hexlify(priv)
    pub = privkey_to_pubkey(priv)
    address = pubkey_to_address(pub)
    signing_tx = signature_form(tx, i, mk_pubkey_script(address), hashcode)
    sig = ecdsa_tx_sign(signing_tx, priv, hashcode)
    txobj = deserialize(tx)
    txobj["ins"][i]["script"] = serialize_script([sig, pub])
    return serialize(txobj)


def signall(tx, priv):
    # if priv is a dictionary, assume format is { 'txinhash:txinidx' : privkey }
    if isinstance(priv, dict):
        for e, i in enumerate(deserialize(tx)["ins"]):
            k = priv["%s:%d" % (i["outpoint"]["hash"], i["outpoint"]["index"])]
            tx = sign(tx, e, k)
    else:
        for i in range(len(deserialize(tx)["ins"])):
            tx = sign(tx, i, priv)
    return tx


def multisign(tx, i, script, pk, hashcode=SIGHASH_ALL):
    if RE_HEX_CHARS.match(tx):
        tx = binascii.unhexlify(tx)
    if RE_HEX_CHARS.match(script):
        script = binascii.unhexlify(script)
    modtx = signature_form(tx, i, script, hashcode)
    return ecdsa_tx_sign(modtx, pk, hashcode)


def apply_multisignatures(*args):
    # tx,i,script,sigs OR tx,i,script,sig1,sig2...,sig[n]
    tx, i, script = args[0], int(args[1]), args[2]
    sigs = args[3] if isinstance(args[3], list) else list(args[3:])

    if isinstance(script, str) and RE_HEX_CHARS.match(script):
        script = binascii.unhexlify(script)
    sigs = [binascii.unhexlify(x) if x[:2] == '30' else x for x in sigs]
    if isinstance(tx, str) and RE_HEX_CHARS.match(tx):
        tx = binascii.unhexlify(tx)
        return safe_hexlify(apply_multisignatures(tx, i, script, sigs))

    txobj = deserialize(tx)
    txobj["ins"][i]["script"] = serialize_script([None]+sigs+[script])
    return serialize(txobj)


def is_inp(arg):
    return (len(arg) > 64 and ':' in arg) or "output" in arg or "outpoint" in arg


def is_outp(arg):
    if isinstance(arg, dict):
        return len(arg) == 2 and 'value' in arg
    elif isinstance(arg, string_types) or ':' in arg:
        addr, value = arg.split(":")
        return bool(is_address(addr)) and 0 < int(value) < 2100000000000000
    else:
        return False


def is_txhex(txhex):
    if not isinstance(txhex, string_or_bytes_types):
        return False
    return bool(RE_TXHEX.match(txhex))


def is_txobj(txo):
    if not isinstance(txo, dict):
        return False
    if "ins" not in txo and "outs" not in txo:
        return False
    if txo.get("ins", []) == [] or txo.get("outs", []) == []:
        return False
    return True


def mktx(*args, **kwargs):
    # [in0, in1...],[out0, out1...] or in0, in1 ... out0 out1 ...
    ins, outs = [], []
    for arg in args:
        if isinstance(arg, list):
            for a in arg: (ins if is_inp(a) else outs).append(a)
        else:
            (ins if is_inp(arg) else outs).append(arg)

    txobj = {"locktime": kwargs.get("locktime", 0), "version": 1, "ins": [], "outs": []}
    for i in ins:
        if isinstance(i, dict) and "outpoint" in i:
            txobj["ins"].append(i)
        else:
            if isinstance(i, dict) and "output" in i:
                i = i["output"]
            txobj["ins"].append({
                "outpoint": {"hash": i[:64], "index": int(i[65:])},
                "script": "",
                "sequence": 0xffffffff
            })
    for o in outs:
        if isinstance(o, string_or_bytes_types):
            addr = o[:o.find(':')]
            val = int(o[o.find(':')+1:])
            o = {}
            if RE_HEX_CHARS.match(addr):
                o["script"] = addr
            else:
                o["address"] = addr
            o["value"] = val

        outobj = {}
        if "address" in o:
            outobj["script"] = address_to_script(o["address"])
        elif "script" in o:
            outobj["script"] = o["script"]
        else:
            raise Exception("Could not find 'address' or 'script' in output.")
        outobj["value"] = o["value"]
        txobj["outs"].append(outobj)

    return serialize(txobj)


def select(unspent, value):
    value = int(value)
    high = [u for u in unspent if u["value"] >= value]
    high.sort(key=lambda u: u["value"])
    low = [u for u in unspent if u["value"] < value]
    low.sort(key=lambda u: -u["value"])
    if len(high):
        return [high[0]]
    i, tv = 0, 0
    while tv < value and i < len(low):
        tv += low[i]["value"]
        i += 1
    if tv < value:
        raise Exception("Not enough funds")
    return low[:i]


# Only takes inputs of the form { "output": blah, "value": foo }
def mksend(*args, **kwargs):
    argz, change, fee = args[:-2], args[-2], int(args[-1])
    ins, outs = [], []
    for arg in argz:
        if isinstance(arg, list):
            for a in arg:
                (ins if is_inp(a) else outs).append(a)
        else:
            (ins if is_inp(arg) else outs).append(arg)

    isum = sum([i["value"] for i in ins])
    osum, outputs2 = 0, []
    for o in outs:
        if isinstance(o, string_types):
            o2 = {
                "address": o[:o.find(':')], "value": int(o[o.find(':')+1:])
            }
        else:
            o2 = o
        outputs2.append(o2)
        osum += o2["value"]

    if isum < osum+fee:
        raise Exception("Not enough money")
    elif isum > osum+fee+5430:
        outputs2 += [{"address": change, "value": isum-osum-fee}]

    return mktx(ins, outputs2, **kwargs)


# script functions

# args = "TxID:index"     or     
# args = txhex, index
# append 'ins' or 'outs' to args: args = "txid:0", 'ins'
def get_script(*args, **kwargs):
    """Extract scripts from txhex; specify 'ins' or 'outs', otherwise default is both"""
    from bitcoin.bci import set_network
    if len(args) > 1 and args[-1] in ("ins", "outs", "both"):
        source = args[-1]
        args = args[:-1]
    else:
        source = "both" 
    
    if is_inp(args[0]):
        txid, vout = args[0].split(':')
        network = set_network(txid) if not kwargs.get('network', None) else kwargs.get('network')
        txo = deserialize(fetchtx(txid, network))    # try fetching txi
    elif len(args) == 3 and source != 'both':
        txhex, vout = str(args[0]), int(args[1])
        txo = deserialize(txhex)                       
    elif len(args) == 2 and source == 'both':
        txhex, vout = str(args[0]), None
        txo = deserialize(txhex)
    elif (1<= len(args) <= 2) and is_txhex(args[0]):    # for no vout
        txo = deserialize(args[0])  
        vout = None
        
    scriptsig, script_pk = [], []
    for inp in txo['ins']:
        scriptsig.append(inp['script'])
    for outp in txo['outs']:
        script_pk.append(outp['script'])
        
    if source == 'ins':                        # return scriptsig
        return scriptsig if not vout else scriptsig[int(vout)]
    elif source == 'outs':                     # return scriptpubkey
        return script_pk if not vout else script_pk[int(vout)]
    elif source == 'both':                     # return BOTH ins & outs
        return {'ins': scriptsig, 'outs': script_pk}
    else:
        raise Exception("Bad source {0} type: choose 'ins', 'outs' or 'both'".format(source))


# takes "txid:vout" or txhex, index
def get_scriptsig(*args, **kwargs):
    """Return scriptSig for 'txid:index'"""
    argz = args + ('ins',)
    if len(args) == 2:
        return get_script(*argz, **kwargs)[int(args[-1])]
    return get_script(*argz, **kwargs)


# takes "txid:vout" or hex_tx, index
def get_scriptpubkey(*args, **kwargs):
    """Return scriptPubKey for 'txid:index'"""
    # TODO: can use biteasy to retrieve a Tx's SPK
    argz = args + ('outs',)
    if len(args) == 2:
        return get_script(*argz, **kwargs)[int(args[-1])]
    return get_script(*argz, **kwargs)



# return inpoints, "TXID:vout", for raw Tx
def get_outpoints(rawtx, i=None):
    """get rawtx's inpoints as 'txid:0' """
    # if isinstance(rawtx, str) and not RE_HEX_CHARS.match(rawtx):    # binary
    #     return safe_unhexlify(get_outpoints(rawtx, i))
    rawtxo = deserialize(rawtx) if (isinstance(rawtx, str) and RE_HEX_CHARS.match(rawtx)) else rawtx
    assert isinstance(rawtx, dict)
    #i = i or int(i)
    outpoints = []
    for tx in multiaccess(rawtxo['ins'], 'outpoint'):
        outpoints.append("%s:%d" % (tx['hash'], tx['index']))
    assert all([is_inp(x) for x in outpoints])
    return outpoints if i is None else outpoints[int(i)]

extract_tx_outpoints = get_outpoints

# https://github.com/richardkiss/pycoin/blob/master/tests/bc_transaction_test.py#L177-L210
# def check_transaction(tx):
#     if isinstance(tx, string_types):
#         if RE_HEX_CHARS.match(tx):
#             txo = json_unhexlify(deserialize(tx))
#         else:
#             txo = deserialize(tx)
#     elif isinstance(tx, dict):
#         txo = json_unhexlify(tx) if json_is_base(tx, 16) else tx
#     else: raise Exception("JSON must be base16)")  # Dict with base256 *values*
#
#     if 'ins' not in txo:
#         raise Exception("TxIns missing")
#     if 'outs' not in txo:
#         raise Exception("TxOuts missing")
#
#     #Size limits
#     MAX_BLOCK_SIZE = 1000000
#     if len(serialize(txo)) > MAX_BLOCK_SIZE:
#         raise Exception("size exceeds MAX BLOCK SIZE: %d" % MAX_BLOCK_SIZE)
#
#     #Check for negative or overflow output values
#     MAX_MONEY = 21000000 * 100000000
#     nValueOut = 0
#     for i, txout in enumerate(txo['outs']):
#         if not (0 <= txout['value'] <= MAX_MONEY):
#             raise Exception("TxOut %d: value negative or out of range" % i)
#         nValueOut += txout['value']
#         if nValueOut > MAX_MONEY:
#             raise Exception("TxOuts' total out of range")
#
#     #Check for duplicate inputs
#     INS = txo['ins']
#     OUTPOINTS = multiaccess(INS, 'outpoint')
#     if len(set(("%s:%d" % (x["hash"], x["index"]) for x in OUTPOINTS))) < len(txo["ins"]):
#         raise Exception("duplicate inputs")
#
#     #Check is coinbase
#     NULL = (b'00'*32, b'\0'*32, 0, 0x80)
#     NEG_ONE = (-1, 0x81, 0xffffffff, b"ff"*4)
#     if len(INS) == 1 and (OUTPOINTS[0]["hash"] in NULL) and (OUTPOINTS[0]["index"] in NEG_ONE):
#         if len(INS[0]["script"]) not in xrange(2, 101):    # script's len 2<=len<=100
#             raise Exception("bad coinbase script size")
#
#     #Check ins aren't missing
#     if not len(INS):        # if len(INS) == 0
#         raise Exception("prevout is null")
#
#     return True

def estimate_tx_size(rawtx):
    # Estimate size of Tx in bytes
    if isinstance(rawtx, basestring) and RE_HEX_CHARS.match(rawtx):
        return estimate_tx_size(deserialize(rawtx))
    outs = rawtx.get("outs", [])
    ins = rawtx.get("ins", [])
    return (len(ins) or 1) * 148 + (34 * len(outs)) + 10


# DER signature related

def deserialize_der(sig):
    """Deserialize DER signature => (r, s, hashcode)"""
    sig = bytes(bytearray.fromhex(sig)) if RE_HEX_CHARS.match(sig) else bytes(bytearray(sig))
    totallen = decode(sig[1], 256) + 2
    rlen = decode(sig[3], 256)
    slen = decode(sig[5+rlen], 256)
    sighashlen = len(sig) - totallen
    r = decode(sig[4:4+rlen], 256)
    s = decode(sig[6+rlen:6+slen+rlen], 256)
    sighash = decode(sig[6+rlen+slen:], 256)
    return r, s, sighash


der_deserialize = deserialize_der


def der_extract_rs(sig):
    r, s, sighash = deserialize_der(sig)
    return r, s


def is_der(sig):
    if not isinstance(sig, string_types):
        return False
    return bool(RE_DER.match(sig))


def der_extract(tx):
    """Extract DERs from a Tx Object"""
    if isinstance(tx, string_types) and RE_TXHEX.match(tx):
        tx = deserialize(tx)
    nin = len(tx.get("ins"))
    ins = tx.get("ins")
    scripts, ders = [], []
    for inp in ins:
        scripts.append(inp.get("script"))
    for scr in scripts:
        descr = deserialize_script(scr)
        der = filter(lambda s: is_der(s), descr)
        ders.extend(der)
    return ders if nin > 1 else ders[0] if nin == 1 else []


get_tx_sigs = extract_ders = der_extract


def mk_opreturn(msg, txhex=None):
    """Makes OP_RETURN script from msg, embeds in Tx hex"""    
    binary_string = binascii.unhexlify(msg)
    hexdata = safe_hexlify(b'\x6a' + num_to_op_push(len(binary_string))) + msg
    if txhex is None:
        return hexdata
    else:
        if isinstance(txhex, string_types) and not RE_TXHEX.match(txhex):
            return unhexlify(mk_opreturn(msg, hexlify(txhex)))
        elif isinstance(txhex, dict):
            txo = txhex
            outs = txo.get('outs')
        else:
            outs = deserialize(txhex).get('outs')
        
        txo = deserialize(txhex)
        assert (len(outs) > 0) and sum(multiaccess(outs, 'value')) > 0 \
                and not any([o for o in outs if o.get("script")[:2] == '6a']), \
            "Tx limited to *1* OP_RETURN, and only whilst the other outputs send funds"
        txo['outs'].append({
                    'script': hexdata, 
                    'value': 0
                    })
        return serialize(txo)
