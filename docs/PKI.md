# Combining an organization's PKI with the passphrase

**Status: design note, not built.** This describes how Lucid Warrant could later use an organization's public key infrastructure: smart cards (PIV/CAC), the Windows certificate store, the macOS Keychain or a PKCS#11 token. Use it if a deployment needs signatures traceable to an enterprise identity, or needs the key protected by hardware. Nothing here changes how warrants work today. Design discussion: issue #2.

## What the passphrase gives you today

Each warrant, the template library, one-time approvals and the organization configuration are signed with an **Ed25519 key**. The key is stored encrypted (scrypt + AES-256-GCM) and unlocked by the analyst's **passphrase**, which is asked at every signature and never cached.

A valid signature therefore proves that **someone who knew the passphrase approved exactly this content**. That's what stops an agent from producing or widening its own warrants. What it doesn't prove, by itself, is **which person** that was, in a way an outside party can check. The key is self-generated, nobody vouches for it, and it can't be revoked centrally.

## What a PKI adds

| Benefit | What it means here |
|---|---|
| **Non-repudiation / attribution** | A signature chains to a certificate the organization issued to a named person, so an auditor can confirm *who* approved a warrant without trusting Lucid Warrant's own key files. |
| **Revocation** | When someone leaves, or a card is lost, the certificate is revoked centrally, and signatures made after that are rejected. |
| **Key protection at rest** | A private key held on a smart card can't be copied off the machine, so stealing the key file isn't enough. |
| **Long-term validity** | With a trusted timestamp (RFC 3161), a signature can be shown to predate a later certificate expiry or revocation. |
| **Policy fit** | Many organizations already require "signed with your card" for approvals; this meets that without a separate process. |

## What it doesn't change (keep the passphrase)

- **The agent runs as the analyst.** Windows (and most smart-card middleware) **caches the card PIN for the logon session**. Once it's unlocked, any process running as the analyst, the agent included, can use the card without a prompt. So a card on its own does **not** stop an agent from signing. The Lucid Warrant passphrase must stay: asked at every signature, never cached, and passphrase-strength (a 4–6 digit PIN can be guessed offline).
- **Exception to confirm with IT:** PIV defines its *digital signature* key (slot 9C) as **PIN-always**: the PIN is required for every signature, not once per session. If the organization's cards and middleware enforce that, the card signature itself becomes a per-signature human step. Verify it on the actual hardware before relying on it.
- **Enforcement still comes from separation.** As README → *What it protects, and what it doesn't* explains, real enforcement means running the executor as a separate user or service that alone holds the search credentials. PKI improves *attribution* and *key custody*, not that.

## Three ways to integrate (in order of effort)

### 1. Key enrollment: bind the Lucid Warrant key to a person (smallest)

Once, when the key is created (or later), the analyst signs a short **enrollment statement** with their enterprise certificate:

```json
{"statement": "lucid-warrant-key-enrollment-v1", "key_id": "<Ed25519 key fingerprint>",
 "public_key": "<PEM>", "warrant_directory": "<project>", "enrolled_at": "<UTC>"}
```

It's stored as `keys/enrollment.json` plus a detached CMS/PKCS#7 signature with the certificate chain and a timestamp. Every Ed25519 signature made with that key is then attributable to the certificate holder, and verifiers check the enrollment once. Warrants, the executor and verification are unchanged. **Limit:** it attributes the key, not each approval. If the key is later revoked, the enrollment shows *who* owned it, but not on its own which warrants came before the revocation (keep the run records and the search service's logs for that).

### 2. Key protection: the card or certificate store unlocks the key (at-rest protection)

The Ed25519 key stays the signing key. What changes is how it's **protected at rest**: in addition to the passphrase layer, it's wrapped with a key that only the card or certificate store can unwrap. Protectors stack, and the list comes from the signed organization configuration, so the agent can't weaken it:

```json
"signing": {"key_protection": ["passphrase", "windows-cert"]}
```

| Protector | Protects against | Notes |
|---|---|---|
| `passphrase` (today) | the agent; a copied key file | always on; asked at every signature |
| `windows-cert` | a copied key file; another machine or user | Windows CNG via `ctypes` (crypt32/ncrypt). A certificate in the user's `MY` store, so a smart card, virtual smart card or software certificate all work the same way. |
| `dpapi` | a copied key file; another Windows user | `CryptProtectData`; unlocked whenever the user is logged in. Weakest; a fallback when there's no certificate. |
| later: `pkcs11`, `macos-keychain` | same | the same interface |

Requirements and cautions:
- **The certificate must allow key wrapping** (key usage `keyEncipherment`/`dataEncipherment`, e.g. PIV slot 9D, key management). Signing-only certificates can't wrap a key.
- **Combine the layers so both are always needed.** Derive one key-encryption key from both secrets (HKDF over the passphrase-derived key and the card-unwrapped secret), so removing either layer doesn't leave the other alone.
- **Re-protecting** after the protector list changes needs the passphrase, and is recorded.

### 3. Dual signatures: each approval also signed with the enterprise certificate (full non-repudiation)

Every signed file gets a **second, detached signature** from the analyst's certificate over the same canonical bytes:

```
data/warrants/w-….json        the warrant
data/warrants/w-….json.sig    Ed25519 (Lucid Warrant's own check; unchanged)
data/warrants/w-….json.p7s    CMS/PKCS#7 detached signature: certificate chain + RFC 3161 timestamp
```

- **Signing:** the passphrase unlocks the Ed25519 key (as now), *then* the card signs (its PIN prompt, per signature with a PIN-always slot). Both must succeed, or nothing is written.
- **Verification policy** (in the signed organization configuration):
  ```json
  "signing": {"pki": {"mode": "dual", "trusted_roots": ["<CA certificate fingerprints>"],
                      "require_timestamp": true, "tsa_url": "https://tsa.example.org",
                      "revocation": "ocsp-at-signing"}}
  ```
  The executor keeps checking Ed25519 for every search, which is fast and offline. The PKI signature is checked at signing and on demand (printing, audits): the certificate chain to a trusted root, revocation at signing time (OCSP or CRL), and the timestamp.
- **The printed warrant** gains a line in IX. Signature: signer's certificate subject, issuer, serial, the timestamp authority's time, and the verification result.
- **Older warrants** (Ed25519 only) stay valid. A policy can require dual signatures only for warrants signed after a date.

## Platform notes

- **Windows:** CNG / CryptoAPI through `ctypes`. Select the certificate from `CurrentUser\My` (`CertOpenStore`, `CryptAcquireCertificatePrivateKey`). Sign with `NCryptSignHash`, or build CMS with `CryptSignMessage`. **Don't shell out to `certutil.exe` or `icacls.exe`**: endpoint-detection tools flag that pattern.
- **macOS:** Security framework / CryptoTokenKit (Keychain, smart cards via the system).
- **Cross-platform:** PKCS#11 (`python-pkcs11`) with the card vendor's module.
- **Verification** of CMS, chains, OCSP and timestamps: the `cryptography` package (already a dependency) handles X.509 parsing and signature checks. Chain building, OCSP and the timestamp protocol need care, or a small, well-reviewed library.

## Testing without real credentials

**Never use the analyst's real signing or PDF certificate, not even for tests**: its signatures are legally attributable, and its use may be logged. On Windows, create a throwaway self-signed certificate in `CurrentUser\My` with a non-exportable key, and delete it afterwards. For CI and macOS, use a fake protector or signer behind the same interface. Test PIN caching explicitly: sign twice and confirm whether the second signature prompts.

## Related: key-file permissions (worth doing regardless)

Found during Windows testing: on a drive root (`E:\`), NTFS gives *Authenticated Users* modify access, and exFAT/FAT drives have no permissions at all. There, other accounts on the machine could read the encrypted key or **replace the public key**. Planned: check and warn in About and at key creation, plus an optional **Restrict access** button (owner, SYSTEM, Administrators; set through the Win32 API).

## Questions to settle before building

1. Which integration is needed: enrollment (1), key protection (2), dual signatures (3), or a combination? Option 1 is a good first step for any of them.
2. Does the organization's card enforce PIN-always on the signature slot, and what's the PIN-caching policy?
3. Which timestamp authority and revocation checks are available from the analyst's machine?
4. Is a certificate with key-encipherment usage available (needed for option 2)?
5. How should About and the tutorial describe which protections are active?
