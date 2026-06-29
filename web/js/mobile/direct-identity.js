/** Phone-as-SSH-client identity for Mobile Direct (not Desktop embedded Hub). */

export const MOBILE_DIRECT_KEY_REF = 'cam_mobile_direct_key_ref';
export const MOBILE_DIRECT_KEY_LABEL = 'cam_mobile_direct_key_label';
export const MOBILE_DIRECT_PUBKEY = 'cam_mobile_direct_pubkey';

export function readMobileDirectIdentity() {
  try {
    return {
      keyRef: localStorage.getItem(MOBILE_DIRECT_KEY_REF) || '',
      keyLabel: localStorage.getItem(MOBILE_DIRECT_KEY_LABEL) || '',
      pubkey: localStorage.getItem(MOBILE_DIRECT_PUBKEY) || '',
    };
  } catch {
    return { keyRef: '', keyLabel: '', pubkey: '' };
  }
}

export function hasMobileDirectIdentity() {
  return !!readMobileDirectIdentity().keyRef;
}

export function saveMobileDirectIdentity({ keyRef, keyLabel, pubkey }) {
  if (keyRef !== undefined) {
    if (keyRef) localStorage.setItem(MOBILE_DIRECT_KEY_REF, keyRef);
    else localStorage.removeItem(MOBILE_DIRECT_KEY_REF);
  }
  if (keyLabel !== undefined) {
    if (keyLabel) localStorage.setItem(MOBILE_DIRECT_KEY_LABEL, keyLabel);
    else localStorage.removeItem(MOBILE_DIRECT_KEY_LABEL);
  }
  if (pubkey !== undefined) {
    if (pubkey) localStorage.setItem(MOBILE_DIRECT_PUBKEY, pubkey);
    else localStorage.removeItem(MOBILE_DIRECT_PUBKEY);
  }
}

export function isPhoneDirectMode() {
  return typeof window !== 'undefined'
    && !(window.CamBridge && typeof window.CamBridge.directHub === 'function');
}
