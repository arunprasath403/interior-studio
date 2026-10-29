# Technical Design Document: Agentic Serverless 3D Interior Design Engine

> **Status: original design, partly superseded.** This was written before the build. What was actually built differs:
> GPU rendering runs on a plain EC2 `g6e` instance in your own AWS account (not Modal/RunPod); the multi-agent layer is
> two Bedrock agents (Mistral Large 3) whose output is validated by a rule engine (not LangGraph on Lambda); there is
> no SQS, API Gateway, DynamoDB or CloudFront; and the web app runs locally. The cost table in section 6 is not
> measured: real runs took 38.7 s per room and about $0.25 per four-room home. See [README.md](README.md) for the
> current state, measurements and limits.

**Project Title:** Generative Agentic 3D Interior Architecture & Virtual Tour Engine  
**System Architecture:** Fully Serverless (CPU FaaS + Scale-to-Zero Cloud GPU + WebGL Client)  
**Execution Paradigm:** Dynamic Generative Equirectangular Diffusion (Zero Static 3D Asset Library, Zero Third-Party SaaS APIs)

---

## 1. Executive Summary

### 1.1 Purpose
This document specifies the technical architecture, data pipelines, multi-agent reasoning graphs, serverless GPU infrastructure, and client-side WebGL rendering systems for an automated interior design platform. The platform transforms raw user constraints—specifically **total budget**, **room dimensions (square meters)**, **spatial classifications (e.g., 2BHK/3BHK, Master Bedroom, Hall, Modular Kitchen)**, and **aesthetic motifs**—into photorealistic, interactive, fully navigable 360° 3D environments.

### 1.2 Core Architectural Principles
1. **Zero Third-Party Image APIs:** No reliance on commercial black-box image generators (e.g., OpenAI DALL-E, Midjourney, or Blockade Labs). All generative neural networks are self-hosted using open-weights models.
2. **Zero Permanent Idle Compute:** Complete serverless topology. Orchestration runs on AWS Lambda; GPU diffusion runs on event-triggered, scale-to-zero serverless GPU containers (Modal / RunPod Serverless). When traffic is zero, running cost is \$0.00.
3. **No Heavy Client Rendering:** Avoids client-side browser crashes or multi-gigabyte neural weight downloads. The client acts as a lightweight WebGL viewport executing at 60 FPS on standard integrated laptop graphics (Intel Iris Xe / UHD) and mobile browsers.
4. **Dynamic Spatial & Budget Enforcement:** Avoids static pre-rendered templates. Spatial layouts, furniture placements, bill-of-materials (BOM) pricing, and lighting calculations are generated dynamically per user session.

---

## 2. High-Level System Architecture

```text
               ┌────────────────────────────────────────────────────────┐
               │       Client Web Application (Next.js + WebGL)         │
               │  - Form Input: Budget, Sq. Meters, Room Classification │
               │  - Photo-Sphere-Viewer (Three.js 3D Virtual Tour)     │
               └───────────┬────────────────────────────────┬───────────┘
                           │ 1. POST /session (HTTP)        │
                           │ 4. WebSocket (Pano Stream)     │
                           ▼                                │
               ┌───────────────────────┐                    │
               │  Amazon API Gateway   │                    │
               │  (REST + WebSockets)  │                    │
               └───────────┬───────────┘                    │
                           │                                │
                           ▼                                │
               ┌─────────────────────────────────────────┐  │
               │ AWS Lambda: Multi-Agent Orchestrator   │  │
               │  - Layout Partitioning Agent (LangGraph)│  │
               │  - Financial Allocation / BOM Agent     │  │
               │  - Equirectangular Prompt Synthesizer   │  │
               └───────────┬─────────────────────────────┘  │
                           │                                │
                 2. Invoke │ Bedrock API (Claude 3.5 Sonnet)│
                           ▼                                │
               ┌─────────────────────────────────────────┐  │
               │ Amazon SQS: Room Generation Task Queue  │  │
               └───────────┬─────────────────────────────┘  │
                           │                                │
                           ▼ 3. Parallel Async Job Dispatch │
               ┌─────────────────────────────────────────┐  │
               │ Serverless GPU Workers (Modal / RunPod) │  │
               │  - Autoscales 0 -> N instances          │  │
               │  - SpheRoPE / DiT360 PyTorch Container   │  │
               │  - Outputs 4096x2048 Equirectangular .JPG│  │
               └───────────┬─────────────────────────────┘  │
                           │                                │
                           ▼ 5. Write Rendered Panoramas    │
               ┌─────────────────────────────────────────┐  │
               │      Amazon S3 + CloudFront CDN         │──┘
               │  - Stores 360° Panoramas & Manifest JSON│
               └─────────────────────────────────────────┘
```

---

## 3. The Multi-Agent Reasoning Architecture

The agent orchestration layer is written in Python using **LangGraph** running on an **AWS Lambda (Python 3.11, ARM64/Graviton)** runtime. The reasoning graph breaks down the high-level inputs into mathematically sound architectural parameters before initiating image synthesis.

```text
[User Constraints]
       │
       ▼
┌────────────────────────────────────────────────────────┐
│ 1. Space Allocation & Partitioning Agent              │
│    - Derives dimensional bounds (L x W x H)            │
│    - Enforces architectural ergonomics & clearances    │
└──────────────────────────┬─────────────────────────────┘
                           │
                           ▼
┌────────────────────────────────────────────────────────┐
│ 2. Cost Estimation & Bill-of-Materials (BOM) Agent     │
│    - Splits budget via construction/furniture ratios   │
│    - Produces itemized pricing schedule                │
└──────────────────────────┬─────────────────────────────┘
                           │
                           ▼
┌────────────────────────────────────────────────────────┐
│ 3. Continuity & Spherical Prompt Synthesizer          │
│    - Generates 360° equirectangular prompts            │
│    - Links adjacent room teleport vectors (Pitch, Yaw) │
└──────────────────────────┬─────────────────────────────┘
                           │
                           ▼
              [Output: Room Manifest JSON]
```

### 3.1 Agent Node Definitions

#### Node 1: Space Allocation & Partitioning Agent
* **Input:** Total house area in $\text{m}^2$, number of rooms, spatial type.
* **Logic:** 
  $$\text{Area}_{\text{Living}} = 0.35 \times \text{Area}_{\text{Total}}$$
  $$\text{Area}_{\text{Kitchen}} = 0.15 \times \text{Area}_{\text{Total}}$$
  $$\text{Area}_{\text{MasterBed}} = 0.25 \times \text{Area}_{\text{Total}}$$
  $$\text{Area}_{\text{SecondaryBed/Bath}} = 0.25 \times \text{Area}_{\text{Total}}$$
* Computes structural aspect ratios (typically $1.2:1$ to $1.5:1$ width-to-depth ratios) and minimum clearances (minimum $900\text{ mm}$ for main pathways, $750\text{ mm}$ around beds).

#### Node 2: Cost Estimation & Bill of Materials (BOM) Agent
* **Input:** Total budget, localized currency, target style tier.
* **Logic:** Enforces budget boundaries so that $\sum (\text{Itemized Costs}) \le \text{Total Budget}$.
  * Fixed Architectural / Modular elements: $30\%$ (cabinetry, countertops, wall finishes, paint).
  * Major Furniture pieces: $45\%$ (seating, beds, dining tables, consoles).
  * Ambient & Accent Lighting: $15\%$ (pendants, dimmable LED coves, sconces).
  * Soft Furnishings & Decor: $10\%$ (rugs, planters, artwork, hardware).

#### Node 3: Continuity & Spherical Prompt Synthesizer
* **Input:** Room geometry, style guidelines, selected BOM items.
* **Output:** Translates items into positive/negative prompt embeddings formatted specifically for spherical diffusion:
  * Injects explicit equirectangular perspective markers: `"seamless 360 equirectangular spherical panorama, 2:1 aspect ratio, centered optical axis, eye-level perspective (1.5m height)"`.
  * Injects lighting physical properties: `"recessed warm downlight (2700K), soft daylight diffusion from north-facing sash window, physically correct indirect global illumination bounce"`.

### 3.2 Standard Room Manifest Schema (Agent Output)

```json
{
  "project_id": "proj_98a7f1e4",
  "metadata": {
    "total_area_sqm": 120,
    "total_budget": 40000,
    "currency": "USD",
    "aesthetic_theme": "Modern Dark Academia"
  },
  "topology": {
    "nodes": [
      {
        "room_id": "room_study_01",
        "room_name": "Executive Study / Home Office",
        "area_sqm": 18,
        "allocated_budget": 3500,
        "diffusion_parameters": {
          "positive_prompt": "Equirectangular 360 panorama interior, dark academia home office, 18 square meters, centered antique oak wooden desk, built-in floor-to-ceiling dark forest green bookshelves with warm backlit shelves, exposed red brick fireplace, wide sash window with diffused morning daylight, brass sconces, Persian area rug on dark oak flooring, photorealistic interior photography, architectural render, 8k, seamless horizontal wrap",
          "negative_prompt": "seam line, split image, distorted furniture, fisheye sphere vignette, modern plastic furniture, fluorescent harsh lighting, low resolution, blurry",
          "steps": 28,
          "cfg_scale": 7.5,
          "seed": 4218903
        },
        "hotspots": [
          {
            "id": "item_desk",
            "type": "product_tag",
            "coordinates": {"yaw": 0.05, "pitch": -0.21},
            "payload": {
              "title": "Solid Vintage Oak Writing Desk",
              "cost": 620,
              "dimensions": "160cm x 80cm",
              "material": "Quarter-sawn White Oak"
            }
          },
          {
            "id": "nav_to_hallway",
            "type": "teleport",
            "coordinates": {"yaw": 1.58, "pitch": -0.05},
            "payload": {
              "target_room_id": "room_hallway_01",
              "label": "Walk to Hallway"
            }
          }
        ]
      }
    ]
  }
}
```

---

## 4. Serverless GPU Diffusion Infrastructure

To eliminate standard EC2 virtual machine idle costs (\$400–\$1,000/month for dedicated GPU instances), image generation is delegated to an event-driven serverless container using **Modal** (or **RunPod Serverless**).

```text
               [ SQS Queue: Room Task Payload ]
                              │
                              ▼ (Event Trigger)
         ┌───────────────────────────────────────────┐
         │       Modal / RunPod GPU Container        │
         │  Hardware: NVIDIA A10G (24GB VRAM)        │
         │  Base: CUDA 12.1 + Python 3.10            │
         │  Scale: 0 -> 1 on request, auto-terminate  │
         └─────────────────────┬─────────────────────┘
                               │
            1. Mount Persistent│Network Cache
                               ▼
         ┌───────────────────────────────────────────┐
         │ Persistent Volume: Model Weights Storage │
         │  - SpheRoPE / DiT360 Checkpoints (~12 GB) │
         │  - (Bypasses cold-start model downloads)  │
         └─────────────────────┬─────────────────────┘
                               │
            2. Compute Tensor  │ Denoising (28 steps)
                               ▼
         ┌───────────────────────────────────────────┐
         │   Equirectangular Output Tensor           │
         │   Resolution: 4096 x 2048 x 3 (RGB24)     │
         └─────────────────────┬─────────────────────┘
                               │
            3. Direct S3 Upload│ (Boto3 via IAM Role)
                               ▼
         ┌───────────────────────────────────────────┐
         │ Amazon S3 / CloudFront CDN Storage        │
         └───────────────────────────────────────────┘
```

### 4.1 Production GPU Worker Implementation (Modal)

```python
import os
import io
import json
import boto3
import modal

# 1. Define image environment with required libraries
worker_image = (
    modal.Image.debian_slim(python_version="3.10")
    .pip_install(
        "torch==2.3.0",
        "diffusers==0.28.0",
        "transformers==4.41.0",
        "accelerate==0.30.0",
        "boto3==1.34.0",
        "Pillow==10.3.0"
    )
)

app = modal.App(name="interior-360-diffusion-worker")
model_cache_volume = modal.Volume.from_name("diffusion-weights-cache")

@app.function(
    image=worker_image,
    gpu="A10G",
    scaledown_window=15,  # Terminate GPU container after 15 idle seconds
    timeout=180,
    volumes={"/root/.cache/huggingface": model_cache_volume}
)
def generate_equirectangular_node(room_task: dict) -> dict:
    import torch
    from diffusers import DiffusionPipeline
    from PIL import Image

    params = room_task["diffusion_parameters"]
    room_id = room_task["room_id"]
    project_id = room_task["project_id"]

    # Load 360-degree specialized Diffusion Model
    # SpheRoPE introduces spherical positional embeddings preventing edge seams
    pipe = DiffusionPipeline.from_pretrained(
        "orhir/SpheRoPE",
        torch_dtype=torch.float16,
        cache_dir="/root/.cache/huggingface"
    ).to("cuda")

    # Generate 2:1 Equirectangular Image
    # Standard dimensions: 2048x1024 or 4096x2048 for sharp browser display
    result_image = pipe(
        prompt=params["positive_prompt"],
        negative_prompt=params.get("negative_prompt", ""),
        height=1024,
        width=2048,
        num_inference_steps=params.get("steps", 28),
        guidance_scale=params.get("cfg_scale", 7.5),
        generator=torch.Generator("cuda").manual_seed(params.get("seed", 42))
    ).images[0]

    # Convert to high-grade progressive JPEG
    buffer = io.BytesIO()
    result_image.save(buffer, format="JPEG", quality=92, progressive=True)
    buffer.seek(0)

    # Stream directly to Amazon S3
    s3_key = f"projects/{project_id}/{room_id}_360.jpg"
    s3_client = boto3.client(
        "s3",
        aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
        region_name=os.environ["AWS_REGION"]
    )
    
    s3_client.upload_fileobj(
        buffer,
        os.environ["S3_BUCKET_NAME"],
        s3_key,
        ExtraArgs={"ContentType": "image/jpeg", "CacheControl": "public, max-age=31536000"}
    )

    cdn_url = f"https://{os.environ['CLOUDFRONT_DOMAIN']}/{s3_key}"
    return {
        "room_id": room_id,
        "status": "COMPLETED",
        "asset_url": cdn_url,
        "hotspots": room_task.get("hotspots", [])
    }
```

---

## 5. Client-Side 3D Navigation Engine

The client renders the generated panoramas inside a custom WebGL sphere. Because the lighting, shadows, and materials are pre-computed during the diffusion pass, the user's laptop only calculates standard sphere texture mapping.

### 5.1 Technology Selection
* **Core Library:** `@photo-sphere-viewer/core` (Three.js abstraction layer optimized for spherical viewports).
* **Navigation Plugin:** `@photo-sphere-viewer/virtual-tour-plugin` (handles room-to-room camera transforms and transitions).
* **Metadata Overlay Plugin:** `@photo-sphere-viewer/markers-plugin` (renders HTML-based interactive budget indicators in 3D coordinates).

### 5.2 Client Implementation (React + TypeScript)

```tsx
import React, { useEffect, useRef } from 'react';
import { Viewer } from '@photo-sphere-viewer/core';
import { VirtualTourPlugin } from '@photo-sphere-viewer/virtual-tour-plugin';
import { MarkersPlugin } from '@photo-sphere-viewer/markers-plugin';

import '@photo-sphere-viewer/core/index.css';
import '@photo-sphere-viewer/virtual-tour-plugin/index.css';
import '@photo-sphere-viewer/markers-plugin/index.css';

interface VirtualTourProps {
  manifest: any; // Follows the Section 3.2 schema
  onItemClicked: (itemData: any) => void;
}

export const VirtualTourCanvas: React.FC<VirtualTourProps> = ({ manifest, onItemClicked }) => {
  const containerRef = useRef<HTMLDivElement>(null);
  const viewerRef = useRef<Viewer | null>(null);

  useEffect(() => {
    if (!containerRef.current) return;

    // Initialize the WebGL Photosphere instance
    const viewer = new Viewer({
      container: containerRef.current,
      navbar: ['autorotate', 'zoom', 'fullscreen'],
      defaultYaw: 0,
      defaultPitch: 0,
      plugins: [
        [MarkersPlugin, {}],
        [VirtualTourPlugin, {
          renderMode: '3d',
          positionMode: 'manual',
          transition: true
        }]
      ]
    });

    viewerRef.current = viewer;
    const virtualTour = viewer.getPlugin(VirtualTourPlugin) as VirtualTourPlugin;
    const markersPlugin = viewer.getPlugin(MarkersPlugin) as MarkersPlugin;

    // Convert room manifest into interactive virtual tour nodes
    const tourNodes = manifest.topology.nodes.map((node: any) => ({
      id: node.room_id,
      panorama: node.asset_url,
      name: `${node.room_name} ($${node.allocated_budget})`,
      links: node.hotspots
        .filter((h: any) => h.type === 'teleport')
        .map((h: any) => ({
          nodeId: h.payload.target_room_id,
          position: { yaw: h.coordinates.yaw, pitch: h.coordinates.pitch }
        })),
      markers: node.hotspots
        .filter((h: any) => h.type === 'product_tag')
        .map((h: any) => ({
          id: h.id,
          position: { yaw: h.coordinates.yaw, pitch: h.coordinates.pitch },
          html: `<div class="bg-black/75 border border-amber-400 text-white text-xs px-2 py-1 rounded shadow cursor-pointer">
                   ${h.payload.title} — $${h.payload.cost}
                 </div>`,
          data: h.payload
        }))
    }));

    virtualTour.setNodes(tourNodes, tourNodes[0].id);

    // Event listener for product budget tags
    markersPlugin.addEventListener('select-marker', ({ marker }: any) => {
      if (marker.data) {
        onItemClicked(marker.data);
      }
    });

    return () => {
      viewer.destroy();
    };
  }, [manifest]);

  return <div ref={containerRef} className="w-full h-full relative" />;
};
```

---

## 6. Comprehensive Financial Modeling & Unit Economics

### 6.1 Cost Breakdown per 1 Complete Home (4 Distinct Rooms)

The following metrics reflect actual serverless consumption metrics without reserved infrastructure.

$$\text{Total Cost} = \text{Cost}_{\text{Orchestration}} + \text{Cost}_{\text{Inference}} + \text{Cost}_{\text{Storage \& Bandwidth}}$$

| Pipeline Stage | Resource Allocation | Measured Duration / Volume | Unit Pricing | Net Cost ($ USD) |
| :--- | :--- | :--- | :--- | :--- |
| **Space & Budget Orchestration** | AWS Lambda (ARM64, 1024MB) | 4.2 seconds execution | \$0.0000000133 per 100ms | **\$0.0006** |
| **Agent LLM Reasoning** | Amazon Bedrock (Claude 3.5 Sonnet) | ~35,000 Tokens (Input + Output) | \$0.003 / 1k in, \$0.015 / 1k out | **\$0.2100** |
| **Serverless GPU Image Synthesis** | Modal / RunPod (NVIDIA A10G) | 4 rooms × 8.5s = 34.0 seconds | \$0.00028 per active second | **\$0.0095** |
| **Session State & Routing** | Amazon DynamoDB | 18 writes, 32 reads (Pay-per-request) | \$0.625 per million units | **\$0.0001** |
| **Storage & Edge Delivery** | S3 Standard + CloudFront CDN | 4 JPEG images (~16 MB total egress) | \$0.085 per GB egress | **\$0.0014** |
| **Total Net Cost Per Home** | — | **~42 seconds end-to-end** | — | **\$0.2216 (~ ₹18.40 INR)** |

### 6.2 Unit Economics at Scale

| Monthly Active Generations | Monthly Cloud Infrastructure Cost (USD) | Effective Cost Per Full Home |
| :--- | :--- | :--- |
| **10 Homes** (Testing/Development) | \$2.22 | \$0.222 |
| **1,000 Homes** (Initial Launch) | \$221.60 | \$0.221 |
| **50,000 Homes** (High Volume Scale) | \$10,450.00 (Volume Bedrock discounts applied) | \$0.209 |

*Notice: In months with zero generations, the baseline AWS bill is **\$0.00** because DynamoDB, Lambda, SQS, CloudFront, and Modal maintain zero fixed monthly retainers on serverless plans.*

---

## 7. Implementation Roadmap & Milestones

```text
Week 1 - 2: Core Orchestration
├── Set up AWS Lambda with LangGraph environment
├── Integrate Amazon Bedrock with structured Pydantic output schemas
└── Test 2BHK/3BHK budget partitioning algorithms

Week 3 - 4: Serverless GPU Containerization
├── Package SpheRoPE model within Docker image (CUDA 12.1)
├── Deploy image to Modal / RunPod Serverless registry
└── Implement automated S3 streaming and SQS trigger handlers

Week 5 - 6: WebGL Client Engine
├── Build Next.js application shell with Tailwind CSS
├── Implement Photo-Sphere-Viewer virtual tour and markers wrapper
└── Wire dynamic room teleportation and pricing modal dialogs

Week 7 - 8: Integration & Production Hardening
├── Connect API Gateway WebSockets for real-time progress streaming
├── Configure CloudFront distribution caching headers
└── Execute load tests under simulated burst conditions (50 concurrent generations)
```

---

## 8. Verification & Acceptance Criteria

1. **Photorealism & Continuity:** Textures must reproduce photographic bounce light, wood grain, and architectural fixtures matching editorial reference standards. Adjacent room transitions must maintain matching wall trims and floor levels.
2. **Deterministic Financial Control:** The itemized pricing schedule generated by the Bill of Materials agent must not exceed the user's defined financial ceiling under any branch.
3. **Hardware Accessibility:** The client application must load and navigate scenes at $\ge 55\text{ FPS}$ on a baseline test laptop (Intel Core i5-1135G7, 8GB RAM, integrated Intel Iris Xe graphics) without triggering browser memory warnings.