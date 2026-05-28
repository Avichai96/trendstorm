/**
 * Fetches /v1/openapi.json from the API and writes TypeScript types to
 * src/api/types.generated.ts using openapi-typescript.
 *
 * Run: npm run codegen
 * CI gate: npm run codegen:check (fails if generated file diverges from committed).
 *
 * The API base URL can be overridden: VITE_API_BASE_URL=http://staging:8080 npm run codegen
 */

import { writeFileSync } from "fs";
import openapiTS, { astToString } from "openapi-typescript";

const base = process.env.VITE_API_BASE_URL ?? "http://localhost:8080";
const schemaUrl = new URL("/v1/openapi.json", base);
const outPath = new URL("../src/api/types.generated.ts", import.meta.url);

console.log(`Fetching OpenAPI schema from ${schemaUrl}…`);

const ast = await openapiTS(schemaUrl, {
  transform(schemaObject) {
    // Map nullable fields to `T | null` (matches Pydantic Optional behaviour)
    if ("nullable" in schemaObject && schemaObject.nullable) {
      return undefined;
    }
  },
});

const header = `/**
 * AUTO-GENERATED — run \`npm run codegen\` to regenerate.
 * Source: GET ${schemaUrl}
 *
 * Manual baseline committed for CI gate. Do not edit by hand.
 */

`;

writeFileSync(outPath.pathname, header + astToString(ast));
console.log(`Written → src/api/types.generated.ts`);
