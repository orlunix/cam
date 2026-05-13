import type { BackendHealth, BackendProfile } from "../lib/types";

interface Props {
  profile: BackendProfile;
  setProfile: (next: BackendProfile) => void;
  health: BackendHealth;
  onRefresh: () => void;
  refreshing: boolean;
}

function formatChecked(iso?: string): string {
  if (!iso) return "";
  try {
    return new Date(iso).toLocaleTimeString();
  } catch {
    return "";
  }
}

export function ConnectionPanel({
  profile,
  setProfile,
  health,
  onRefresh,
  refreshing,
}: Props) {
  const checked = formatChecked(health.checkedAt);
  const statusLabel = health.ok
    ? health.version || "connected"
    : health.error || "disconnected";

  return (
    <section className="panel connection">
      <header className="connection-header">
        <div>
          <h1>Cam Desktop</h1>
          <p
            className={health.ok ? "status-line ok" : "status-line err"}
            title={statusLabel}
          >
            <span className="dot" aria-hidden="true" />
            <span className="status-text">{statusLabel}</span>
          </p>
          {checked ? <p className="checked-at">last check {checked}</p> : null}
        </div>
        <button onClick={onRefresh} disabled={refreshing}>
          {refreshing ? "..." : "Refresh"}
        </button>
      </header>

      <label>
        Profile
        <select
          value={profile.kind}
          onChange={(e) => {
            const kind = e.target.value;
            if (kind === "local") setProfile({ kind: "local", camcPath: "camc" });
            if (kind === "wsl") setProfile({ kind: "wsl", camcPath: "camc" });
            if (kind === "ssh")
              setProfile({ kind: "ssh", host: "", camcPath: "~/.cam/camc" });
          }}
        >
          <option value="local">local</option>
          <option value="wsl">wsl</option>
          <option value="ssh">ssh</option>
        </select>
      </label>

      {profile.kind === "ssh" ? (
        <>
          <label>
            Host
            <input
              value={profile.host}
              onChange={(e) => setProfile({ ...profile, host: e.target.value })}
              placeholder="host.example.com"
            />
          </label>
          <label>
            User
            <input
              value={profile.user || ""}
              onChange={(e) => setProfile({ ...profile, user: e.target.value })}
              placeholder="optional"
            />
          </label>
          <label>
            Port
            <input
              type="number"
              value={profile.port ?? ""}
              onChange={(e) => {
                const value = e.target.value;
                setProfile({
                  ...profile,
                  port: value ? Number(value) : undefined,
                });
              }}
              placeholder="22"
            />
          </label>
        </>
      ) : null}

      {profile.kind === "wsl" ? (
        <label>
          Distro
          <input
            value={profile.distro || ""}
            onChange={(e) => setProfile({ ...profile, distro: e.target.value })}
            placeholder="default"
          />
        </label>
      ) : null}
    </section>
  );
}
