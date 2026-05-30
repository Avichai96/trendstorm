import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";

const ACTION_CODE = `exports.onExecutePostLogin = async (event, api) => {
  const namespace = "https://trendstorm.ai/";
  const roles = event.authorization?.roles ?? [];
  const tenant_id = event.user.app_metadata?.tenant_id ?? null;

  api.idToken.setCustomClaim(namespace + "roles", roles);
  api.accessToken.setCustomClaim(namespace + "roles", roles);

  if (tenant_id) {
    api.idToken.setCustomClaim(namespace + "tenant_id", tenant_id);
    api.accessToken.setCustomClaim(namespace + "tenant_id", tenant_id);
  }
};`.trim();

export default function Settings() {
  return (
    <div className="space-y-6 max-w-2xl">
      <div>
        <h1 className="text-2xl font-bold">Settings</h1>
        <p className="text-sm text-muted-foreground">
          Platform configuration and Auth0 setup guide.
        </p>
      </div>

      <Card>
        <CardHeader>
          <div className="flex items-center gap-2">
            <CardTitle className="text-base">Auth0 Post-Login Action</CardTitle>
            <Badge variant="outline" className="text-xs">Required</Badge>
          </div>
        </CardHeader>
        <CardContent className="space-y-4 text-sm">
          <p className="text-muted-foreground">
            TrendStorm reads roles and tenant membership from JWT claims injected by an Auth0
            Action. Without this action, role-gated pages (Reviews, Audit Log) will be inaccessible
            even for users with the correct role assignments.
          </p>

          <ol className="list-decimal list-inside space-y-2 text-muted-foreground">
            <li>
              In your Auth0 dashboard, go to{" "}
              <strong className="text-foreground">Actions → Library</strong> and click{" "}
              <strong className="text-foreground">Build Custom</strong>.
            </li>
            <li>
              Name it <strong className="text-foreground">Inject TrendStorm Claims</strong>, choose
              trigger <strong className="text-foreground">Login / Post Login</strong>, runtime{" "}
              <strong className="text-foreground">Node 18</strong>.
            </li>
            <li>Paste the code below into the action editor.</li>
            <li>
              Click <strong className="text-foreground">Deploy</strong>, then go to{" "}
              <strong className="text-foreground">Actions → Flows → Login</strong> and drag the
              action into the flow between "Start" and "Complete".
            </li>
          </ol>

          <div className="space-y-1">
            <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
              Action code
            </p>
            <pre className="rounded-md bg-muted p-4 text-xs font-mono overflow-x-auto whitespace-pre-wrap break-all">
              {ACTION_CODE}
            </pre>
          </div>

          <div className="rounded-md border border-amber-200 bg-amber-50 dark:border-amber-800 dark:bg-amber-950/20 p-3 text-xs text-amber-800 dark:text-amber-200">
            <strong>Note:</strong> The <code>tenant_id</code> claim is populated from
            <code> app_metadata.tenant_id</code>. This is set automatically when a user joins an
            organization via invitation or self-registration. Users who signed up before this field
            existed may need a manual metadata update via the Auth0 Management API.
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">API Access</CardTitle>
        </CardHeader>
        <CardContent className="text-sm text-muted-foreground space-y-2">
          <p>
            Use <strong className="text-foreground">API Keys</strong> for programmatic access from
            CI pipelines, scripts, or the Python SDK. API keys carry the same tenant scope as the
            user who created them.
          </p>
          <p>
            The Python SDK can be installed via <code className="text-foreground">pip install trendstorm</code>{" "}
            and configured with an API key for full read/write access.
          </p>
        </CardContent>
      </Card>
    </div>
  );
}
