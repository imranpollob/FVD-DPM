# FVD-DPM: Project Summary and Architecture Analysis

## Overview
**FVD-DPM** (Fine-grained Vulnerability Detection via Conditional Diffusion Probabilistic Models) is a deep learning-based vulnerability detection system published at USENIX Security '24. The project implements a novel approach that treats vulnerability detection as a **diffusion-based graph-structured prediction problem**.

## Core Innovation
Unlike traditional binary classification approaches (vulnerable vs. non-vulnerable), FVD-DPM performs:
1. **Slice-level detection**: Identifies which program slices contain vulnerabilities
2. **Statement-level localization**: Pinpoints exact vulnerable code lines within those slices

This is achieved by modeling the problem as a **conditional diffusion process over graph structures**, specifically applying Denoising Diffusion Probabilistic Models (DDPM) to code graphs.

---

## Architecture Components

### 1. Data Representation: Graph-based Vulnerability Candidates (GrVCs)

#### What are GrVCs?
GrVCs are **program slices extracted from code** that combine multiple graph representations:
- **Control Flow Graph (CFG)**: Captures program execution flow
- **Program Dependency Graph (PDG)**: Captures data and control dependencies
- **Call Graph (CG)**: Captures function call relationships

#### GrVCs Extraction Pipeline (`data_preprocess/`)
The preprocessing follows a 5-step process using Joern (v0.3.1) and Neo4j:

```
Source Code → Joern Parser → Neo4j Database → GrVCs Extraction
```

**Step-by-step process:**
1. **CFG Generation** (`get_cfg_relation.py`)
   - Extracts control flow relationships
   - Identifies branching and loop structures

2. **PDG Completion** (`complete_PDG.py`)
   - Builds program dependency graphs
   - Captures data flow and control dependencies

3. **Call Graph Construction** (`access_db_operate.py`)
   - Maps function call relationships
   - Enables inter-procedural analysis

4. **Entry Node Extraction** (`entry_nodes_get.py`)
   - Identifies slicing criteria:
     - Sensitive API calls (762 APIs from `API-library function calls.txt`)
     - Sensitive variables (pointers, arrays)
     - Arithmetic expressions
   - These become starting points for program slicing

5. **GrVCs Extraction** (`extract_GrVCs.py`)
   - Performs backward and forward program slicing
   - Creates graph representations with:
     - **Nodes**: AST nodes with type and code attributes
     - **Edges**: Dependency relationships (control/data flow)
     - **Labels**: Binary labels (0=safe, 1=vulnerable) for each node
     - **Line numbers**: Maps nodes to source code lines

#### GrVCs Data Structure
```json
{
  "filename": "path/to/source.c",
  "nodes": [node_ids],
  "nodes_label": [node_types],
  "nodes_codes": [code_snippets],
  "edges": [[src, dst], ...],
  "node_target": [0/1 labels],
  "code_lines": [line_numbers]
}
```

---

### 2. Feature Extraction (`preprocess.py`)

#### Node Embedding Generation
Each node is represented by combining:

**a) Type Embedding (1-dim)**
- One-hot encoding of AST node types
- Example types: Function, Statement, Expression, Identifier, etc.
- Stored in `token_index` dictionary

**b) Code Embedding (128-dim)**
- Uses **Word2Vec (Skip-gram)** to encode code tokens
- Tokenization via custom tokenizer (`my_tokenizer.py`)
  - Lexical analysis with state machine
  - Extracts: keywords, operators, identifiers, literals
  - Handles C/C++ syntax specifics
- Token sequences → Word2Vec model → 128-dim vectors
- Sum pooling over token embeddings for each code snippet

**Final Node Feature**: `[type_emb (1-dim) | code_emb (128-dim)]` = **129-dim vector**

#### Data Splitting
- Training: 80%
- Validation: 10%
- Testing: 10%
- Fixed block size: 200 nodes (configurable, paper uses 400)
- Padding with zeros for smaller graphs

---

### 3. Diffusion Model Architecture

The model is inspired by **DPM-SNC** (Diffusion Probabilistic Models for Semantic Node Classification) but adapted for vulnerability detection on code graphs.

#### 3.1 Denoising Model (`denoising_model.py`)

**Core Idea**: Learn to predict node labels by iteratively denoising from random noise.

**Architecture:**
```
Input: Node features X, Noisy labels Y_t, Adjacency matrix A, Timestep t
       ↓
  [Time Embedding Layer]
       ↓
  [GAT Layer 1] ← Time conditioning
       ↓
  [GAT Layer 2] ← Time conditioning
       ↓
  [MLP Output Layer]
       ↓
Output: Predicted noise/denoised labels
```

**Key Components:**

1. **Time Encoding**
   - **Absolute Time**: Sinusoidal positional embedding (similar to Transformers)
   - **Relative Time**: Learnable positional embeddings
   - Encoded as: `t_emb = MLP(sin/cos(t))` with dimension 32 (default nhid)

2. **Graph Attention Network (GAT) Layers** (`layers.py`)
   - Multi-head attention mechanism (6 heads by default)
   - Custom GAT implementation with time conditioning
   - Attention computation: `α = softmax((x_i * W_i + t_emb) · (x_j * W_j + t_emb))`
   - Propagates information along graph edges
   - Number of layers: 2 (configurable)

3. **Skip Connections** (optional)
   - Residual connections between layers
   - Helps gradient flow in deeper networks

4. **Output Layer** (MLP)
   - Projects graph embeddings to label space
   - Outputs 2 values per node: [μ, log(σ²)] for Gaussian distribution
   - Then predicts noise for denoising

**Parameters:**
- Input features: 129 (1 type + 128 code)
- Hidden dimension: 32
- Number of labels: 2 (vulnerable/safe)
- Number of nodes: 200 per graph
- Attention heads: 6
- Number of layers: 2

---

#### 3.2 Diffusion Process (`gaussian_ddpm_losses.py`)

**Theoretical Foundation**: DDPM (Denoising Diffusion Probabilistic Models)

The model learns a **reverse diffusion process** to recover clean labels from noise:

**Forward Process (Fixed):**
```
q(y_t | y_0) = N(y_t; √(ᾱ_t)y_0, (1-ᾱ_t)I)
```
- Gradually adds Gaussian noise to ground truth labels y_0
- After T steps, y_T ≈ pure noise
- Uses linear beta schedule: β_t ∈ [0.0001, 0.0008]
- Number of timesteps: 40 (configured in `config.yaml`)

**Reverse Process (Learned):**
```
p_θ(y_{t-1} | y_t, X, A) = N(y_{t-1}; μ_θ(y_t, X, A, t), Σ_θ(y_t, X, A, t))
```
- Neural network predicts noise ε_θ to denoise
- Iteratively refines from y_T → y_0
- Conditioned on node features X and graph structure A

**Loss Function (Hybrid):**
```
L = L_VLB + λ · L_MSE
```

1. **Variational Lower Bound (VLB)**:
   - KL divergence: `D_KL(q(y_{t-1}|y_t,y_0) || p_θ(y_{t-1}|y_t))`
   - Ensures learned distribution matches true posterior

2. **MSE Loss** (weighted or unweighted):
   - `||ε - ε_θ(y_t, X, A, t)||²`
   - Direct noise prediction loss
   - Weighted by noise schedule (controlled by `unweighted_MSE` flag)

**Training Strategy:**
- Time batch: 8 (trains on 8 different timesteps simultaneously)
- Random timestep sampling each iteration
- Distributed training support (PyTorch DDP)

---

#### 3.3 Inference (`gaussian_ddpm_losses.test()`)

**Sampling Process:**
```python
# Start from random noise
y_T = N(0, noise_temp)  # noise_temp = 0.001

# Iteratively denoise for T steps
for t = T → 1:
    ε_θ = model(X, y_t, A, t)
    y_{t-1} = (1/√α_t)(y_t - (1-α_t)/√(1-ᾱ_t) * ε_θ) + √β_t * noise
    
# Final prediction: argmax(y_0)
```

**Why it works:**
- Each step removes a small amount of predicted noise
- Graph structure guides denoising (via GAT layers)
- Node features provide context
- Multi-step refinement improves accuracy

---

### 4. Training Pipeline (`trainer.py`)

#### Training Loop
```
For each epoch:
  For each batch:
    1. Sample timestep t ~ Uniform(1, T)
    2. Add noise to ground truth: y_t = √ᾱ_t * y_0 + √(1-ᾱ_t) * ε
    3. Predict noise: ε_θ = model(X, y_t, A, t)
    4. Compute loss: L = VLB_loss + weighted_MSE_loss
    5. Backpropagate and update
  
  Every 10 epochs:
    - Evaluate on train/validation sets
    - Compute metrics: F1, Recall, AUC, MCC, IOU
    - Save best model (based on F1 + IOU)
```

#### Evaluation Metrics

**Statement-level Metrics:**
- **F1 Score**: Harmonic mean of precision/recall
- **Recall**: Ability to find vulnerable statements
- **AUC**: Area under ROC curve
- **MCC**: Matthews Correlation Coefficient (handles class imbalance)

**Line-level Metrics (Novel):**
- **IOU (Intersection over Union)**:
  ```
  IOU = |predicted_vuln_lines ∩ true_vuln_lines| / 
        |predicted_vuln_lines ∪ true_vuln_lines|
  ```
  - Maps node predictions to source code lines
  - Measures localization accuracy
  - Ignores padding nodes (line = -1)

**Key Innovation**: The IOU metric enables **vulnerability localization**, not just detection.

---

### 5. Configuration (`config.yaml`)

```yaml
data:
  nfeat: 129          # Node feature dimension
  nlabel: 2           # Binary classification

diffusion:
  method: Gaussian    # Diffusion type
  step: 40           # Diffusion timesteps

model:
  nhid: 32           # Hidden dimension
  num_layers: 2      # GAT layers
  num_linears: 2     # MLP output layers

train:
  num_epochs: 15000
  batch: 32
  block_size: 400    # Nodes per graph
  time_batch: 16      # Timesteps per training iteration
  lr: 0.005
  lr_schedule: True  # Exponential decay
  lr_decay: 0.9999999
  grad_norm: 0.1     # Gradient clipping
```

---

## Datasets

The project supports 5 vulnerability datasets:

1. **NVD (National Vulnerability Database)**
   - Real-world CVE vulnerabilities
   - C/C++ code from various projects

2. **SARD (Software Assurance Reference Dataset)**
   - Synthetic vulnerable code samples
   - Covers CWE vulnerability types

3. **OpenSSL**
   - Real vulnerabilities from OpenSSL library
   - Cryptographic code vulnerabilities

4. **Libav**
   - Multimedia library vulnerabilities
   - Buffer overflows, use-after-free

5. **Linux Kernel**
   - Kernel-level vulnerabilities
   - Privilege escalation, race conditions

**Data Structure:**
```
data/
├── {dataset}_GrVCs.json          # Raw GrVCs
├── {dataset}_train.json          # Preprocessed train
├── {dataset}_valid.json          # Preprocessed validation
└── {dataset}_test.json           # Preprocessed test
```

---

## Code Structure Mapping

### File Organization
```
FVD-DPM/
├── main.py                    # Entry point, DDP setup
├── trainer.py                 # Training/testing loop
├── denoising_model.py        # GAT-based denoising network
├── gaussian_ddpm_losses.py   # Diffusion process & loss
├── preprocess.py             # Feature extraction (Word2Vec)
├── layers.py                 # GAT & MLP implementations
├── config.yaml               # Hyperparameters
├── parser.py                 # Argument parser
├── my_tokenizer.py          # C/C++ code tokenizer
├── requirements.txt          # Dependencies
│
├── data_preprocess/          # GrVCs extraction pipeline
│   ├── get_cfg_relation.py
│   ├── complete_PDG.py
│   ├── access_db_operate.py
│   ├── entry_nodes_get.py
│   ├── extract_GrVCs.py
│   ├── slice_op.py           # Slicing algorithms
│   └── general_op.py         # Utility functions
│
├── utils/
│   ├── loader.py             # Model/data/optimizer loaders
│   ├── data_loader.py        # PyTorch Geometric DataLoader
│   └── logger.py             # Logging utilities
│
└── data/
    ├── API-library function calls.txt  # 762 sensitive APIs
    └── {dataset}/
        ├── {dataset}_train.json
        ├── {dataset}_valid.json
        └── {dataset}_test.json
```

### Execution Flow
```
1. Data Preprocessing (One-time):
   preprocess.py → Word2Vec training → {dataset}_*.json

2. Training:
   main.py → Parser → Config loading
         ↓
   Trainer.train() → DataLoader → Batching
         ↓
   gaussian_ddpm_losses.loss_fn()
         ↓
   denoising_model.forward() → GAT layers → MLP
         ↓
   Loss computation → Backprop → Optimizer step
         ↓
   Evaluation (F1, IOU) → Save best model

3. Testing:
   main.py --do_train test
         ↓
   Load saved model → Inference (40-step denoising)
         ↓
   Metrics computation (test set)
```

---

## Key Technical Details

### 1. Graph Attention Mechanism
- **Why GAT?** Captures dependency relationships in code graphs
- **Time Conditioning**: Attention scores influenced by diffusion timestep
- **Multi-head**: 6 parallel attention mechanisms capture different aspects

### 2. Diffusion for Graphs
- **Challenge**: Graphs have variable structure (unlike images)
- **Solution**: Fixed block size (200 nodes) with padding
- **Graph conditioning**: Adjacency matrix A guides message passing

### 3. Distributed Training
- PyTorch DDP (DistributedDataParallel)
- Supports multi-GPU training
- Configurable via `--nproc_per_node` argument

### 4. Program Slicing
- **Backward slicing**: Traces data/control dependencies upstream
- **Forward slicing**: Traces impact downstream
- **Sensitive criteria**: APIs, pointers, arithmetic (potential vuln. sites)

---

## Paper Alignment

### Methodology Match
✓ **GrVCs Extraction**: Matches paper's description of combining CFG, PDG, CG  
✓ **Diffusion Model**: Implements DDPM for node classification  
✓ **GAT Architecture**: Uses graph attention for message passing  
✓ **Dual-level Detection**: Slice-level (graph classification) + Statement-level (node labels)  
✓ **Metrics**: F1, Recall, AUC, MCC for detection + IOU for localization  

### Implementation Corresponds to:
- **Section 3.1** (Paper): GrVCs extraction → `data_preprocess/`
- **Section 3.2** (Paper): Node embeddings → `preprocess.py`
- **Section 3.3** (Paper): Conditional diffusion → `gaussian_ddpm_losses.py`
- **Section 3.4** (Paper): Denoising network → `denoising_model.py`
- **Section 4** (Paper): Experiments → `trainer.py` + `config.yaml`

---

## Dependencies

### Core Libraries
- **PyTorch 1.12.1** (CUDA 11.3): Deep learning framework
- **PyTorch Geometric 2.1.0**: Graph neural network library
- **torch-scatter/sparse**: Efficient graph operations
- **Gensim**: Word2Vec implementation
- **tqdm**: Progress bars
- **PyYAML/EasyDict**: Configuration management

### External Tools (Data Preprocessing)
- **Joern 0.3.1**: Code parser (Java-based)
- **Neo4j 2.1.5**: Graph database
- **py2neo 2021.2.3**: Python-Neo4j connector

---

## Usage Summary

### 1. Setup Environment
```bash
conda create -n FVD-DPM python=3.10
conda install pytorch==1.12.1 cudatoolkit=11.3 -c pytorch
pip install torch-scatter torch-sparse torch-geometric==2.1.0
pip install tqdm pyyaml easydict gensim scikit-learn pandas
```

### 2. Preprocess Data
```bash
# Generate node embeddings
python preprocess.py  # Outputs: data/{dataset}_*.json
```

### 3. Train Model
```bash
# Single GPU
python -m torch.distributed.run --nproc_per_node 1 main.py --dataset openssl

# Multi-GPU (e.g., 4 GPUs)
python -m torch.distributed.run --nproc_per_node 4 main.py --dataset NVD
```

### 4. Test Model
```bash
python -m torch.distributed.run --nproc_per_node 1 main.py \
    --dataset openssl --do_train test
```

### 5. Outputs
- **Models**: `out_models/{dataset}/model.bin`
- **Logs**: `logs_train/{dataset}/Gaussian/`
- **Metrics**: F1, Recall, AUC, MCC, IOU (printed + logged)

---

## Research Contributions

### Novel Aspects
1. **First to apply DDPM to vulnerability detection**
   - Treats prediction as generative process
   - Iterative refinement improves accuracy

2. **Fine-grained localization**
   - Node-level labels enable line-level vulnerability pinpointing
   - IOU metric quantifies localization quality

3. **Graph-level slicing criteria**
   - GrVCs combine multiple graph views (CFG+PDG+CG)
   - Slicing from sensitive operations (APIs, pointers)

4. **Conditional diffusion on graphs**
   - Time conditioning in GAT layers
   - Graph structure guides denoising process

### Advantages over Prior Work
- **vs. SySeVR**: Uses program slicing but only binary classification
- **vs. VulDeePecker**: Token-based, misses graph structure
- **vs. Devign**: Graph-level only, no statement localization
- **FVD-DPM**: Graph structure + Fine-grained labels + Diffusion refinement

---

## Potential Improvements & Extensions

### Identified Opportunities
1. **Dynamic block sizing**: Current fixed 200 nodes may truncate large functions
2. **Attention visualization**: GAT attention weights could explain predictions
3. **Cross-dataset evaluation**: Train on one dataset, test on another
4. **Hyperparameter tuning**: Timesteps (40), hidden dim (32), heads (6)
5. **Code transformations**: Data augmentation (variable renaming, etc.)
6. **Real-time inference**: Optimize sampling speed (fewer timesteps?)

### Code Quality Notes
- Well-structured with clear separation of concerns
- Comprehensive preprocessing pipeline
- Configurable architecture via YAML
- Distributed training support
- Some hardcoded values (e.g., `world_size=1` in main.py)
- Custom tokenizer handles C/C++ specifics well

---

## Conclusion

FVD-DPM represents a **paradigm shift** in vulnerability detection:
- From **classification** → **generative modeling**
- From **binary labels** → **fine-grained localization**
- From **heuristics** → **learned diffusion process**

The codebase is a complete implementation matching the USENIX Security '24 paper, with:
- ✅ Comprehensive data preprocessing (GrVCs extraction)
- ✅ Feature engineering (Word2Vec embeddings)
- ✅ Novel diffusion-based architecture (GAT + DDPM)
- ✅ Dual-level evaluation (detection + localization)
- ✅ Multi-dataset support (NVD, SARD, OpenSSL, Libav, Linux)

The project demonstrates how **diffusion models** can be effectively adapted to **graph-structured code analysis**, opening new research directions in **AI for software security**.
