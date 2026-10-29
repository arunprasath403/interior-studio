# Interior Studio

**Describe a home in plain words. Get a priced room-by-room design and a photorealistic 360° walkthrough you can explore and share.**

### 🔗 Live demo: <https://main.d27svicx4cn44w.amplifyapp.com>

Open the sample homes, look around each room, and tap any piece to see its price. Creating a *new* home is by invitation
while the studio is in preview, because each one runs a GPU on the owner's AWS bill.

![Three homes designed by the studio](docs/hero.jpg)

## How it works

1. **Plan.** AI designers (Amazon Bedrock, Mistral Large 3) read your brief, lay out the rooms and choose priced items.
   A rule engine checks every answer and guarantees the total never exceeds your budget.
2. **Design each room.** A GPU machine on AWS runs DiT360 on FLUX.1-dev and paints one 2048×1024 equirectangular
   panorama per room (about 39 seconds each), repairs the wrap-around seam, and shuts itself down.
3. **Walk through it.** A 360° viewer with room-to-room navigation and a price on every item.

The site shows the real progress as it happens: *Planning your home → Getting the design studio ready → Designing the
living room → Designing the kitchen → Designing the master bedroom → Designing the bedroom → Adding the finishing touches.*

## Sample outputs

Everything below was produced by the live system. Each folder in [`docs/samples`](docs/samples) has the full 360°
panoramas (`*-360.jpg`), a flat view of each room (`*-view.jpg`) and the plan with every item and price (`plan.json`).

### A ₹2 lakh, "Casagrand-style" 2-bedroom apartment

> *"2 bedroom apartment, about 88 sq m (950 sq ft), 2 lakh rupees for the interiors, modern contemporary style like a
> Casagrand apartment, simple and practical"*

Planned in 4 seconds; rendered on a live GPU in about 6 minutes (about $0.19 of GPU time). ₹1,55,183 planned of the
₹2,00,000 budget at the "Essential" level. [Plan with all items and prices](docs/samples/casagrand-style-2-lakh/plan.json).
*Casagrand is a real developer; this is an independent interpretation of a modern contemporary apartment look and is not
affiliated with or endorsed by them. Typical Casagrand 2-bedroom units are roughly 840-1,080 sq ft.*

<p>
<img src="docs/samples/casagrand-style-2-lakh/living-room-view.jpg" width="49%" alt="Living room">
<img src="docs/samples/casagrand-style-2-lakh/kitchen-view.jpg" width="49%" alt="Kitchen">
<img src="docs/samples/casagrand-style-2-lakh/master-bedroom-view.jpg" width="49%" alt="Master bedroom">
<img src="docs/samples/casagrand-style-2-lakh/bedroom-view.jpg" width="49%" alt="Second bedroom">
</p>

### A ₹30 lakh warm Scandinavian family home

2 bedrooms, 90 m², no TV in the living room. [Plan](docs/samples/scandinavian-30-lakh/plan.json)

<p>
<img src="docs/samples/scandinavian-30-lakh/living-room-view.jpg" width="49%" alt="Living room">
<img src="docs/samples/scandinavian-30-lakh/kitchen-view.jpg" width="49%" alt="Kitchen">
<img src="docs/samples/scandinavian-30-lakh/master-bedroom-view.jpg" width="49%" alt="Master bedroom">
<img src="docs/samples/scandinavian-30-lakh/bedroom-view.jpg" width="49%" alt="Second bedroom">
</p>

### A ₹12 lakh calm Japandi studio

1 bedroom, 55 m². [Plan](docs/samples/japandi-12-lakh/plan.json)

<p>
<img src="docs/samples/japandi-12-lakh/living-room-view.jpg" width="32%" alt="Living room">
<img src="docs/samples/japandi-12-lakh/kitchen-view.jpg" width="32%" alt="Kitchen">
<img src="docs/samples/japandi-12-lakh/master-bedroom-view.jpg" width="32%" alt="Master bedroom">
</p>

**Full 360° panorama** (this is the image the viewer maps onto a sphere):

![Casagrand-style living room, full 360 panorama](docs/samples/casagrand-style-2-lakh/living-room-360.jpg)

## The site

<p>
<img src="docs/screens/home.jpg" width="49%" alt="Home page">
<img src="docs/screens/gallery.jpg" width="49%" alt="Gallery of sample homes">
<img src="docs/screens/plan.jpg" width="49%" alt="The design plan with a price for every room">
<img src="docs/screens/progress.jpg" width="49%" alt="Step-by-step progress while the rooms are designed">
<img src="docs/screens/walkthrough-live.jpg" width="49%" alt="Walkthrough with priced items">
<img src="docs/screens/walkthrough-phone.jpg" width="49%" alt="Walkthrough on a phone-width screen">
</p>

The wording is plain on purpose: a test fails if a word like "GPU" or "token" appears in anything a visitor can read.

## Measured, not estimated

| | Result |
|---|---|
| One room, 2048×1024, on an NVIDIA L40S | **38.7 s** (steady state) |
| Click to finished walkthrough, 4 rooms, cold start | **about 6.5 minutes**, about **$0.19-0.25** |
| System RAM during model loading | **6.6-12.6 GB** (the repo's own loader needed 30+ GB and froze a 32 GiB machine twice; see below) |
| Seam repair (CPU, about 0.1 s per image) | wrap-around seam score 5.1 → 0.7, 4.6 → 0.4, 3.3 → 0.25 on real renders |
| Tests | 145 (budget limits, access control, every job state, site wording) |

## Architecture

```text
Visitor's browser ── Amplify Hosting (site/)                        static pages, HTTPS, security headers
        │
        └──► API Gateway (HTTP API, throttled) ──► Lambda (backend/) ──► DynamoDB   designs, jobs, limits
                                                        │  ├──► Bedrock             plans a home from words
                                          EventBridge ──┘  └──► EC2 GPU machine     paints the rooms, then stops
                                          (every minute)             │
                                                                     └──► S3 (private)  rooms; the site gets
                                                                          short-lived links to show them
```

| Folder | What it is |
|---|---|
| `engine/` | Layout, budget, prompts, currency, and the Bedrock agents. The budget ceiling is enforced here, never by the model. |
| `generator/` | DiT360 renderer, low-RAM model loader, seam detection and repair. |
| `backend/` | The cloud API and the render job state machine (Lambda + DynamoDB). `backend/dev.py` runs it all locally with no AWS. |
| `site/` | The website (no build step). |
| `infra/` | CloudFormation template, the one-shot GPU launcher, and the deploy script. |
| `app/`, `web/`, `viewer/` | An earlier local-only studio and a standalone viewer, kept as developer tools. |
| `tests/` | 145 tests. |

### Things that went wrong, and what fixed them

- **The panorama model froze a 32 GiB GPU machine, twice.** The model's reference loader reads all 33 GB of weights into
  system RAM first. Loading straight onto the GPU (`device_map="cuda"`) cut peak RAM from 30+ GB to 6.6 GB, so the cheaper
  machine now works. The per-minute memory log added to every run is what made the cause visible.
- **A dependency conflict made the first GPU run fail at install time.** Pinning `diffusers==0.35.2` fixed it, and an
  import check now runs before the 30 GB download so this kind of failure is cheap.
- **The first plans left most of the money unspent.** A flat price level used only 13% of a $40,000 budget, and a rupee
  budget was treated as dollars and used about 2%. Prices are now fitted to the budget per category and converted by
  currency; the live ₹2 lakh sample spends 78% (the catalog's cheapest level is the limit there).
- **Browsers could not submit the create form.** The CORS pre-check hit a catch-all route and returned 404; found in the
  live deployment and fixed with a regression test.

## Run it yourself

Needs Python 3.12 (`pip install pillow boto3`).

**Preview the whole site locally with no AWS** (real pages and sample panoramas, a simulated render of about a minute):

```bash
python -m backend.dev          # http://127.0.0.1:8770, invitation code: demo-code
```

**Deploy to your own AWS account.** This creates an Amplify app, a CloudFormation stack (API Gateway, Lambda, DynamoDB,
an EventBridge rule, IAM roles), and a CORS rule on your bucket.

```bash
python -m infra.setup --profile YOUR_PROFILE --apply       # one private S3 bucket + one IAM role for the GPU machine
aws s3 cp hf-token.txt s3://ai-interior-3d-<account-id>/secrets/hf-token --profile YOUR_PROFILE
python -m infra.deploy --profile YOUR_PROFILE all          # prints the site address and an invitation code, once
```

Before the first render: accept the [FLUX.1-dev licence](https://huggingface.co/black-forest-labs/FLUX.1-dev) on
Hugging Face and put a read token in `hf-token.txt`. Your profile needs Bedrock access to Mistral Large 3 and the ability
to launch a `g6e` GPU instance (request the quota first; AWS often has no capacity, and the site retries for up to 20
minutes). Tests: `python -m unittest discover -s tests -t .`

## Safety and cost control

The public site can start paid GPUs, so spending is limited on the server, not in the browser:

- Browsing samples and opening a shared link are public. Creating a design or starting a render needs an invitation code.
- One render at a time, 6 renders and 100 plans per day, API throttling, a 40-minute ceiling per render, and a dead-man
  switch that powers the GPU machine off regardless.
- The Lambda role can only terminate machines tagged for this project and cannot read the Hugging Face token. The GPU
  machine's role can read and write one bucket and nothing else.
- Failed renders show a kind message; the real reason stays in the job record. Set an AWS Budgets alarm anyway.

## Known limits

- **Product tags are not on the furniture.** Diffusion does not say where it drew each object, so tags are spread around
  the room by category. Use the item list as the source of truth. Prices come from a small fixed catalog and are estimates.
- **The catalog is small.** The agents choose from it, so "lots of plants" gets one herb planter, and budgets far above
  about ₹90,000 per m² leave money unspent. ₹2 lakh for a full 2-bedroom home is tight, so that sample is "Essential" level.
- **Rooms are rendered independently.** One shared style helps, but matching floors and trims across rooms is not
  guaranteed, and the same seed and similar prompts give similar compositions (you can see the same window in several samples).
- **Seam repair is classical image processing.** It hides steps and small shifts near the join; it cannot make mismatched
  objects line up. Currency rates are fixed and approximate (`engine/currency.py`).
- **GPU capacity is the main operational risk.** AWS often has no `g6e` capacity; Spot quota exists only in one region.
- The images are AI-generated for inspiration. They are not photographs, plans, quotes or a promise of what will be built.

## Licensing

Code: MIT (see [LICENSE](LICENSE)). Model weights are not included. DiT360's weights are MIT but run on FLUX.1-dev, whose
licence is **non-commercial**: fine for a portfolio and research, not for a paid service without a commercial licence from
Black Forest Labs. The sample images were generated with FLUX.1-dev; its licence allows using outputs for any purpose except
training a competing model. [`project_documentation.md`](project_documentation.md) is the original design, partly superseded
by what is described here.
