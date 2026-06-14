"""
_wallet_validation.py — Multi-chain wallet address validation for the Wallet
Collection feature (inside the Engage module).

validate_wallet(address, chain) returns (is_valid, normalized_or_error). On a
valid address the second item is the normalized address ready to store; on an
invalid one it is a short human readable reason shown back to the member.

These are format checks, not on-chain existence checks. They reject obviously
wrong input (wrong prefix, wrong length, wrong alphabet) so a project owner's
collected list stays clean. The 'other' chain accepts anything non-trivial so
admins can collect for chains not in the explicit list.
"""
import re

# Pre-compiled so the click path never recompiles on every submission.
_RE_EVM     = re.compile(r'0x[a-fA-F0-9]{40}')
_RE_SOLANA  = re.compile(r'[1-9A-HJ-NP-Za-km-z]{32,44}')
_RE_COSMOS  = re.compile(r'[a-z]+1[a-z0-9]{30,80}')
_RE_HEX0X   = re.compile(r'0x[a-fA-F0-9]{1,64}')   # aptos / sui

# The set of chains the dashboard and slash commands expose. Kept here so the
# API can validate the blockchain field against a single source of truth.
SUPPORTED_CHAINS = (
    'evm', 'solana', 'bitcoin', 'cardano',
    'cosmos', 'tron', 'aptos', 'sui', 'other',
)

# Friendly labels for embeds / dashboard badges.
CHAIN_LABELS = {
    'evm':     'EVM',
    'solana':  'Solana',
    'bitcoin': 'Bitcoin',
    'cardano': 'Cardano',
    'cosmos':  'Cosmos',
    'tron':    'Tron',
    'aptos':   'Aptos',
    'sui':     'Sui',
    'other':   'Other',
}


def validate_wallet(address: str, chain: str) -> tuple[bool, str]:
    """Returns (is_valid, normalized_address_or_error_message)."""
    address = (address or '').strip()
    if not address:
        return False, 'Please enter a wallet address.'

    if chain == 'evm':
        if _RE_EVM.fullmatch(address):
            return True, address.lower()   # normalize to lowercase
        return False, 'EVM wallet must start with 0x and be 42 characters long.'

    if chain == 'solana':
        if _RE_SOLANA.fullmatch(address):
            return True, address
        return False, 'Solana wallet must be 32 to 44 base58 characters.'

    if chain == 'bitcoin':
        if (address.startswith('1') and 26 <= len(address) <= 35) or \
           (address.startswith('3') and 26 <= len(address) <= 35) or \
           (address.startswith('bc1') and 42 <= len(address) <= 62):
            return True, address
        return False, 'Bitcoin address must start with 1, 3, or bc1.'

    if chain == 'cardano':
        if (address.startswith('addr1') or address.startswith('stake1')) and len(address) >= 50:
            return True, address
        return False, 'Cardano address must start with addr1 or stake1.'

    if chain == 'cosmos':
        if _RE_COSMOS.fullmatch(address):
            return True, address
        return False, 'Cosmos address must be bech32 format (e.g. cosmos1...).'

    if chain == 'tron':
        if address.startswith('T') and len(address) == 34:
            return True, address
        return False, 'Tron address must start with T and be 34 characters.'

    if chain == 'aptos':
        if _RE_HEX0X.fullmatch(address):
            return True, address.lower()
        return False, 'Aptos address must start with 0x and be hex.'

    if chain == 'sui':
        if _RE_HEX0X.fullmatch(address):
            return True, address.lower()
        return False, 'Sui address must start with 0x and be hex.'

    if chain == 'other':
        if len(address) >= 4:
            return True, address
        return False, 'Address too short.'

    return False, f'Unsupported chain: {chain}'
