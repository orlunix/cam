import { useCallback, useMemo, useState } from "react";
import { CamcCliBackend } from "./camcClient";
import type { CamBackend } from "./camBackend";
import type { BackendProfile } from "./types";

const STORAGE_KEY = "cam.desktop.profile";

const DEFAULT_PROFILE: BackendProfile = {
  kind: "local",
  camcPath: "camc",
};

function loadProfile(): BackendProfile {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return DEFAULT_PROFILE;
    return JSON.parse(raw) as BackendProfile;
  } catch {
    return DEFAULT_PROFILE;
  }
}

function persistProfile(profile: BackendProfile): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(profile));
  } catch {
    // localStorage may be unavailable in some contexts; ignore.
  }
}

export interface UseProfile {
  profile: BackendProfile;
  setProfile: (next: BackendProfile) => void;
  backend: CamBackend;
}

export function useProfile(): UseProfile {
  const [profile, setProfileState] = useState<BackendProfile>(() => loadProfile());

  const setProfile = useCallback((next: BackendProfile) => {
    setProfileState(next);
    persistProfile(next);
  }, []);

  const backend = useMemo<CamBackend>(() => new CamcCliBackend(profile), [profile]);

  return { profile, setProfile, backend };
}
