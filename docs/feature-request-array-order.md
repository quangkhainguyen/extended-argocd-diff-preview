# Feat | Support opt-in order-insensitive array comparison via JSONPath

## Problem

ArgoCD Diff Preview currently compares rendered resources as serialized YAML.
Changing only the order of array entries therefore produces added/deleted lines
and marks the resource and application as modified, even when the ordering has
no operational significance for those particular entries.

This commonly affects independent environment variables, volumes, and volume
mounts. Helm chart updates, generators, and refactoring can reorder these arrays
without changing their contents. In repositories with hundreds or thousands of
Applications, the resulting noise obscures real changes and makes pull request
review harder.

This request is **not** to treat every Kubernetes array as unordered.

## Minimal reproduction

Render the following Deployment in the base branch:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: example
spec:
  selector:
    matchLabels:
      app: example
  template:
    metadata:
      labels:
        app: example
    spec:
      containers:
        - name: app
          image: nginx:1.27
          env:
            - name: LOG_LEVEL
              value: info
            - name: DB_HOST
              value: postgres
```

In the target branch, change only `env` to:

```yaml
env:
  - name: DB_HOST
    value: postgres
  - name: LOG_LEVEL
    value: info
```

Run a preview using the existing rendering workflow.

**Actual:** The Deployment and application are reported as modified, with
added/deleted lines for the moved entries.

**Expected when explicitly configured:** No resource diff, no application
modification caused solely by this reorder, and no added/deleted summary lines.
Without the new configuration, preserve the current behavior.

## Proposed configuration

Introduce a list of JSONPaths selecting arrays whose order may be ignored.
For example:

```sh
argocd-diff-preview \
  --ignore-array-order '.spec.template.spec.containers[*].env,.spec.template.spec.volumes,.spec.template.spec.containers[*].volumeMounts'
```

Equivalent environment-variable configuration:

```sh
export IGNORE_ARRAY_ORDER='.spec.template.spec.containers[*].env,.spec.template.spec.volumes,.spec.template.spec.containers[*].volumeMounts'
```

These names and examples describe a **proposed interface**, not an existing flag.

The default list should be empty. Exact paths should distinguish, for example,
`containers` from `initContainers`. Document the supported JSONPath subset,
list delimiters, and configuration precedence. At minimum, support nested field
access and array wildcards. Invalid or unsupported expressions should produce
an actionable error rather than silently broadening the selection.

Pod paths start at `.spec`, while CronJob paths start at
`.spec.jobTemplate.spec.template.spec`; selection must use the complete manifest
structure rather than the visible lines in a diff hunk.

## Required comparison semantics

- Ignore ordering only at selected arrays; preserve all entry contents.
- Compare full objects, including nested `valueFrom` references, rather than
  matching only `name` or dropping entries with the same name.
- Preserve multiplicity: adding or deleting a duplicate entry is a change.
- Keep arrays belonging to different containers or resources separate.
- Preserve actual value changes, additions, deletions, mount-path changes,
  volume-source changes, and Secret/ConfigMap reference changes.
- Keep unselected arrays order-sensitive, even if a selected array is nearby.
- Do not recursively treat nested arrays as unordered unless separately selected.
- Handle absent or empty arrays without manufacturing changes.
- Do not mutate the extracted manifests or change the resources submitted for
  rendering. Normalization should operate on comparison copies.
- Apply consistent semantics before changed-resource detection and throughout
  application matching, resource diff generation, and summary calculation.
- Ensure Markdown and HTML report the same remaining changes and counts.
- Keep application renames and real resource additions/deletions visible.

Canonicalizing complete selected array entries before comparison is one possible
implementation. It should not require a new runtime executable or a Dockerfile
dependency. Benchmark the implementation against realistic application counts.

## Safety considerations

Array order is not universally irrelevant, including within `env`:

- `$(VAR)` expansion can depend on previously defined variables.
- Duplicate variable names can make ordering significant.
- `command` and `args` are ordered command-line sequences.
- `envFrom` source order can determine precedence for overlapping keys.
- `initContainers` execution order is significant.

The feature must therefore remain opt-in, with documented warnings. Selecting
`initContainers[*].env` must not implicitly ignore the order of `initContainers`
itself. Users remain responsible for selecting arrays where order is irrelevant
for their manifests.

## Reproduction tests included with this request

[`array_order_test.go`](../pkg/matching/array_order_test.go) includes an
intentionally failing regression test, `TestArrayOrder_ReversedEnvProducesNoDiff`.
Its inputs contain exactly two unchanged environment variables in reversed
order. It expects no diff, but the current implementation reports two added and
two deleted lines.

Run this test without a cluster or Argo CD CLI:

```sh
go test ./pkg/matching/... -run TestArrayOrder -count=1
```

This regression test intentionally makes the current test suite fail. It
expresses the desired comparison result, not an implementation of the feature.
If the feature is implemented as opt-in, the test will need to enable it.

## Acceptance criteria for a future implementation

1. With no configuration, all existing comparison behavior remains unchanged.
2. With a matching JSONPath, the reorder-only reproduction above produces no
   resource diff and no spurious modified application or summary counts.
3. A path selecting `containers[*].env` does not suppress `initContainers[*].env`,
   `args`, `command`, `envFrom`, or arrays in another manifest location.
4. Mixed reorder/content changes report the content changes without ordering
   noise. Additions, deletions, and duplicate multiplicity remain visible.
5. Test exact-path matching, wildcard expansion across multiple containers,
   missing fields, empty lists, nested selected arrays, and malformed paths.
6. Test that unselected nested arrays retain order sensitivity and that multiple
   selected paths work together without cross-container/resource matching.
7. Verify both output formats and summary counts, including rename-only apps,
   resources added/deleted in full, and inputs unchanged after comparison.
8. Provide configuration parsing tests, realistic-scale performance coverage,
   user documentation, and safety warnings.

The proposed configuration-dependent acceptance tests cannot pass until the
native feature is implemented; the included characterization tests do not claim
to validate suppression that does not yet exist.
