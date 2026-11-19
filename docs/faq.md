# Visitor FAQ

Questions someone reading this repository is likely to ask, with short answers and
links to the details.

## Why four repositories and not one?

Each project solves a problem on its own: a research agent, a model router, an
evaluation harness, and the operations around one model. Each has its own tests, CI,
dependencies and release history, and each can be used without the others (Dyno can
evaluate any agent behind HTTP; Gearbox can route any OpenAI-format client). This
repository holds only what belongs to the combination: the pinned versions, one command
to run everything, and the system test.

## Why do they only talk over HTTP, command lines and files?

So no project depends on another's internals. A Python import would tie their
dependency versions together, and they already conflict: Gearbox pins `anthropic<2`
(through LiteLLM) while Dyno needs `anthropic>=2.3`. Gearbox therefore runs Dyno through
Dyno's own command line in Dyno's own virtual environment. Every connection is an API or
file format with a test on both sides (for example Gearbox's `/metrics` contract, which
Pitwall's dashboards and alerts read).

## What does pinning commits buy?

A record of which four versions were tested together. `stack pins` shows when a local
checkout has moved away from its pin; `stack pins --update` moves the pins on purpose,
in a commit that CI then checks: every pinned commit must exist on GitHub, and the
system job builds and starts exactly those commits.

## Why run native processes on Windows and Docker Compose in CI?

The machine the projects were built on has no Docker, and the services start in seconds
as plain processes from each project's own virtual environment, so `stack up` does that.
Compose is how the system would run on a Linux server, so CI proves the Compose file
works on every push: it builds the four pinned commits, starts them with Postgres and
Redis, and runs the smoke checks.

## Why doesn't Scout go through Gearbox by default?

Routing Scout through Gearbox moves its planner from Sonnet to Haiku. On 3 questions it
halved the cost per report and covered the same key points, but the Haiku planner asks
fewer sub-questions, so reports kept fewer claims
([report](https://github.com/Utsav0706/gearbox/blob/main/reports/scout.md)), and 3
questions prove little. Step I5 runs Scout's full
50-question golden set directly and through Gearbox, judged by Dyno with the same
calibrated judges, and the default changes only if quality holds. Until then it is one
flag: `stack up --scout-via-gearbox`.

## The router model doesn't beat "always use the medium model". Isn't that a failure?

It is the finding. Gearbox's own routers, Pitwall's fine-tuned model and a logistic
regression all reached the same result: which tier a prompt truly needs is barely
predictable from its text, and the medium model (Haiku) is good enough for most prompts.
The cost saving comes from not sending everything to the large model. What Pitwall
demonstrates is the operations loop around a model (versioned data, training, gate,
canary, monitoring, drift, retraining, rollback), and that works whatever the model's
accuracy.

## How are the results kept honest?

- Judges are calibrated against human labels (Dyno reports Cohen's kappa: task rubric
  1.00, quality 0.95, hallucination 0.63) and are blind to which system produced an
  answer.
- Reports state their sample sizes and intervals; the test sets are small (119 router
  prompts, 50 research questions), so a few points either way is noise.
- Before/after comparisons use repeated attempts (pass^k), because the same Scout
  commit scored 70% once and 47% over three attempts on the same 10 cases.
- Costs are counted twice where possible (Gearbox's request log and Dyno's records
  agreed to the penny on the I3 run).

## What does it cost to run?

Locally, nothing beyond the paid model calls. Gearbox's test mix costs about £0.32 per
1,000 requests (against £4.52 for always using Sonnet); a Scout report about £0.024.
Building and evaluating all four cost a little over £10 in recorded API calls; the
README has the breakdown.

## How much memory does the whole system need?

About 2.5 GB for the six core services (Grafana and MLflow are the largest), plus Ollama
and its models. The three Streamlit apps (`stack up --ui`) add about 200 MB each.

## What would deploying it look like?

All four go to the cloud together, deployed on demand (start, use, tear down) rather
than kept running, with a budget alarm. Scout already has its production Compose file,
migrations and image builds; that plan is the step after I5.
