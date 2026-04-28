# SPIR Dynamic Extraction — Claude System Guide

## 🎯 Project Goal

This system extracts structured **BILL OF MATERIAL** data from SPIR Excel files.

The extraction MUST be:
- Fully dynamic (NO hardcoding)
- Format-independent
- Production-safe
- Accurate across ALL file combinations

---

## 📂 SPIR File Structure

Each SPIR file contains a combination of:

1. Main Sheet (MANDATORY)
2. Continuation Sheet (OPTIONAL)
3. Annexure Sheet (OPTIONAL)

---

## 🧠 Core Principle

> ALL extraction must be driven by structure detection — NOT fixed positions.

---

# 🟡 1. MAIN SHEET

### Purpose:
Contains:
- Spare details
- Tag numbers (direct OR via annexure reference)
- Model numbers
- Serial numbers

### Rules:

- At least ONE main sheet exists in every file
- Tags may be:
  - Directly present
  - OR referenced via annexure

---

### 🔹 Tag Handling

Tags may appear as:

| Format | Expected Output |
|--------|----------------|
| `23 to 24` | 23, 24 |
| `23V01-A/B` | 23V01-A, 23V01-B |
| `25V01-34,35,45` | Expanded with prefix |

👉 MUST expand into individual TAG INSTANCES  
❌ DO NOT treat as a single value  
❌ DO NOT treat as multiple sheets  

---

### 🔹 Critical Rule

> ALWAYS identify TAG column FIRST before extracting anything else.

---

# 🔵 2. CONTINUATION SHEET

### Purpose:
- Extends main sheet data
- Maps items using ITEM NUMBER

---

### Rules:

IF continuation sheet contains:
- Only item mapping → use MAIN sheet for spare details
- Full spare details → extract directly

---

### 🔹 Mapping Logic

- ITEM NUMBER is the PRIMARY KEY
- NEVER rely on row order

---

### 🔹 Important Constraint

❌ DO NOT blindly increment line numbers  
✅ ALWAYS use ITEM NUMBER mapping

---

# 🟢 3. ANNEXURE SHEET

### Purpose:
Contains:
- Tag numbers
- Model numbers
- Serial numbers
- Many details (but mostly we need these three only)

Used when:
- Tag count is large
- Main/Continuation references annexure

---

### Types:

1. Separate sheets (Annexure-1, Annexure-2)
2. Combined annexure list in single sheet

---

### Rules:

- Annexure is ALWAYS a reference layer
- NEVER contains spare details

---

### 🔹 Tag Extraction

Tags may appear:
- One per row
- OR multiple in one cell (comma separated)

👉 MUST expand ALL tags

---

### 🔹 Linking Rule

- Use reference like:
  - "Refer Annexure 1"
- Map to correct annexure block

---

### 🔴 Critical Constraint

❌ NEVER treat "ANNEXURE 1" as a tag  
❌ NEVER put annexure reference in tag column  

---

# 🔁 EXTRACTION FLOW (STRICT)

1. Detect sheet type
2. Identify TAG source:
   - Direct column
   - Column headers
   - Annexure reference
3. Expand tags
4. Extract spare data
5. Link continuation (via ITEM NUMBER)
6. Resolve annexure mapping
7. Generate final rows

---

# ⚠️ DATA INTEGRITY RULES

### Column Purity

- TAG column → ONLY tags
- MODEL column → ONLY model numbers
- SERIAL column → ONLY serial numbers

❌ NO cross contamination

---

# 🧾 SPIR TYPE LOGIC (CRITICAL)

There are ONLY 4 valid types:

- COMMISSIONING SPARES
- INITIAL SPARES
- NORMAL OPERATING SPARES
- LIFE CYCLE SPARES

---

### Detection Rules:

- Exactly ONE type will be selected (tick/1/true)
- Labels may be far from tick (scan dynamically)

---

### Mandatory Behavior:

✅ Return detected type ONLY if explicitly selected  

---

### Strict Constraints:

❌ DO NOT default to any type  
❌ DO NOT guess  
❌ DO NOT return first match  

---

### Edge Cases:

| Scenario | Output |
|--------|--------|
| No SPIR type section | NULL |
| Labels exist but no tick | NULL |
| Multiple detected but no clear selection | NULL |

---

# 🧩 EDGE CASES

### Multi-main sheets
- Process independently
- Merge results

---

### Multi-annexure sheets
- Resolve using reference number

---

### Large tag counts
- Expand fully before mapping

---

# 🚫 ANTI-HARDCODING RULES

❌ No fixed row limits (like row 8, row 15)  
❌ No fixed column assumptions  
❌ No project-specific logic  
❌ No fallback defaults that alter meaning  

---

# ✅ SUCCESS CRITERIA

System should:

- Work for ANY SPIR file format
- Correctly extract tags (no pollution)
- Correctly map annexure
- Correctly detect SPIR type
- Produce zero incorrect defaults

---

# 🔍 VALIDATION BEFORE COMPLETE

Before marking extraction complete:

- Verify tag count matches expected
- Ensure no "ANNEXURE" values in tag column
- Ensure SPIR type is correct or NULL
- Ensure no duplicated rows due to expansion errors

---

# 🧠 FINAL PRINCIPLE

> If logic depends on position → it's fragile  
> If logic depends on structure → it's scalable  

Build ONLY structure-driven logic.