# Stateful Code Execution REPL

The `execute_code` tool gives the agent a real REPL — variables,
imports, and mutated objects survive across calls within one session.
Three runtimes are supported:

| Runtime   | Stateful? | Persisted across calls | Notes                              |
| --------- | --------- | ---------------------- | ---------------------------------- |
| `python`  | **yes**   | namespace pickle       | Survives until `reset:true`        |
| `node`    | no        | —                      | Stateless one-shot                 |
| `shell`   | no        | —                      | Stateless one-shot                 |

## How Python state survives

After each Python call, the executor pickles the current namespace to:

```
<workspace>/.mini_cc/repl/<session_id>.python.pickle
```

Before the next call, the pickle is loaded back so the agent sees its
previous variables, imports, and object mutations. Unpicklable values
(functions, file handles, locks, etc.) are silently filtered out — they
don't survive, but they don't break the call either.

## `reset: true`

```python
execute_code(runtime="python", code="x = 1", reset=True)
```

Drops the persisted pickle and starts from a fresh namespace. Useful
when the previous state has grown too large or when the agent wants a
clean slate for a different sub-task.

## Routing through the sandbox

Every call goes through `ctx.sandbox.execute()`:

- **Subprocess tenants**: runs as a host subprocess.
- **Container tenants**: transparently routed through
  `docker exec -w /workspaces/<pid>` — the agent code doesn't know the
  difference.

## Usage examples

```python
# 1. Define a DataFrame
execute_code(runtime="python", code="""
import pandas as pd
df = pd.DataFrame({'x': [1,2,3], 'y': [4,5,6]})
df['z'] = df['x'] + df['y']
""")

# 2. Inspect it on the next call — df is still here
execute_code(runtime="python", code="print(df.describe())")

# 3. Node (stateless)
execute_code(runtime="node", code="console.log(1 + 2)")

# 4. Shell (stateless)
execute_code(runtime="shell", code="ls -la")
```

## Caveats

- **Picklability**: objects that hold OS resources (open files, DB
  connections, threads) won't survive across calls. They still work
  *within* a single call.
- **Concurrency**: not safe to call concurrently from multiple
  teammates on the same session — they share one pickle file.
- **Size**: a namespace holding gigabytes of data will be re-pickled
  on every call. Drop large objects explicitly before the call returns
  if this becomes a problem.
