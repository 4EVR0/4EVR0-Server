# Neo4j Graph DB 실제 스키마

> 탐색일: 2026-05-18  
> 서버: `bolt://100.72.139.8:7687` (Neo4j Community 5.26.26, Tailscale)

---

## 1. 노드 현황

| 레이블 | 수량 | 주요 속성 |
|--------|------|----------|
| `Ingredient` | 2,967개 | `ingredient_id`, `inci_name`, `kor_name`, `cosing_functions[]` |
| `Product` | 2,565개 | - |
| `Effect` | 15개 | `effect_code`, `effect_name_en` |
| `Concern` | 15개 | `concern_code`, `concern_name_ko` |

---

## 2. 관계 구조

```
(Product) -[:CONTAINS]→ (Ingredient) -[:AFFECTS]→ (Effect) -[:RELATES_TO]→ (Concern)
```

### AFFECTS 관계 속성

| 속성 | 타입 | 설명 |
|------|------|------|
| `graph_score` | Float | 근거 강도 점수 (높을수록 강함) |
| `evidence_type` | String | `pubmed_evidence` (논문 근거) \| `cosing_function` (성분 기능 기반) |
| `paper_count` | Int | 관련 논문 수 |
| `type` | String | 관계 유형 (예: `reduces`, `promotes`) |

> **우선순위**: `pubmed_evidence` > `cosing_function`, 동점 시 `graph_score DESC`

---

## 3. Effect 코드 전체 목록 (15개)

| effect_code | effect_name_en | 한국어 의미 |
|-------------|---------------|------------|
| `ANTI_INFLAMMATORY` | Anti-inflammatory | 항염 |
| `SOOTHING` | Soothing | 진정 |
| `BARRIER_REPAIR` | Barrier repair | 장벽 강화 |
| `HYDRATING` | Hydrating | 수분 공급 |
| `MOISTURE_RETENTION` | Moisture retention | 수분 유지 |
| `SEBUM_REGULATION` | Sebum regulation | 피지 조절 |
| `ANTIMICROBIAL` | Antimicrobial | 항균 |
| `KERATOLYTIC` | Keratolytic | 각질 용해 |
| `COMEDOLYTIC` | Comedolytic | 면포 용해 |
| `WOUND_HEALING` | Wound healing | 상처 회복 |
| `DEPIGMENTING` | Depigmenting | 색소 억제 |
| `BRIGHTENING` | Brightening | 미백/광채 |
| `ANTIOXIDANT` | Antioxidant | 항산화 |
| `PHOTOPROTECTIVE` | Photoprotective | 광보호 |
| `ANTI_AGING` | Anti-aging | 노화 방지 |

---

## 4. Concern 코드 전체 목록 (15개)

| concern_code | concern_name_ko |
|-------------|----------------|
| `ACNE` | 여드름 |
| `COMEDONES` | 면포 |
| `OILY_SKIN` | 지성 피부 |
| `SENSITIVE_SKIN` | 민감성 피부 |
| `REDNESS` | 붉은기 |
| `IRRITATED_SKIN` | 자극 피부 |
| `DRY_SKIN` | 건성 피부 |
| `DEHYDRATED_SKIN` | 수분 부족 피부 |
| `BARRIER_DAMAGE` | 피부 장벽 손상 |
| `HYPERPIGMENTATION` | 색소침착 |
| `DULLNESS` | 피부 톤 저하 |
| `POST_ACNE_MARKS` | 여드름 자국 |
| `ATOPIC_PRONE` | 아토피 피부 경향 |
| `ROSACEA_PRONE` | 주사 피부 경향 |
| `AGING_SIGNS` | 노화 징후 |

> ⚠️ DB의 Concern은 15개로 코드의 Concern enum(26개)보다 적음  
> DB에 없는 concern: `PORE_CONGESTION`, `ENLARGED_PORES`, `FLAKY_SKIN`, `ROUGH_TEXTURE`,  
> `UNEVEN_SKIN_TONE`, `BLEMISHES`, `DARK_CIRCLES`, `SUNBURN`, `WRINKLES`, `LOSS_OF_ELASTICITY`, `SAGGING_SKIN`

---

## 5. 기존 코드 vs 실제 스키마 불일치 요약 (수정 완료)

| 항목 | 기존 코드 (잘못됨) | 실제 DB | 수정 |
|------|------------------|---------|------|
| Effect 노드 키 | `{name: ...}` | `{effect_code: ...}` | ✅ |
| 관계 (성분→효과) | `[:HAS_EFFECT]` | `[:AFFECTS]` | ✅ |
| Claim 노드 | `(c:Claim)` | 없음 (관계 속성으로 대체) | ✅ |
| Paper 노드 | `(p:Paper)` | 없음 (관계 속성으로 대체) | ✅ |
| eligibility_tier | `c.eligibility_tier` | `r.evidence_type` | ✅ |
| paper_ref | `p.paper_ref` | `r.paper_count` | ✅ |
| PORE_MINIMIZING Effect | enum에 존재 | DB에 없음 → SEBUM_REGULATION/KERATOLYTIC으로 대체 | ✅ |
| EXFOLIATING Effect | enum에 존재 | DB에 없음 → KERATOLYTIC으로 대체 | ✅ |
| SMOOTHING Effect | enum에 존재 | DB에 없음 → MOISTURE_RETENTION으로 대체 | ✅ |
| FIRMING Effect | enum에 존재 | DB에 없음 → ANTI_AGING으로 대체 | ✅ |

---

## 6. 수정된 Cypher 쿼리

```cypher
UNWIND $effects AS effect_code
MATCH (i:Ingredient)-[r:AFFECTS]->(e:Effect {effect_code: effect_code})
RETURN DISTINCT
    i.inci_name        AS name,
    i.kor_name         AS kor_name,
    e.effect_name_en   AS claim,
    r.evidence_type    AS eligibility_tier,
    toString(r.paper_count) AS paper_ref,
    r.graph_score      AS graph_score
ORDER BY
    CASE r.evidence_type WHEN 'pubmed_evidence' THEN 0 ELSE 1 END,
    r.graph_score DESC,
    i.inci_name
LIMIT 20
```

---

## 7. 검증 결과 (2026-05-18)

`BARRIER_REPAIR`, `HYDRATING`, `ANTI_AGING` effects 조회 → **10개 성분 정상 반환**

| 성분 (INCI) | 한글명 | 효과 | 근거 | 논문 수 | 점수 |
|------------|--------|------|------|---------|------|
| COLLOIDAL OATMEAL | 콜로이달오트밀 | Barrier repair | pubmed_evidence | 4 | 0.867 |
| POTASSIUM LACTATE | 포타슘락테이트 | Hydrating | pubmed_evidence | 2 | 0.718 |
| ASCORBIC ACID | 아스코빅애씨드 | Anti-aging | pubmed_evidence | 1 | 0.693 |
| PETROLATUM | 페트롤라툼 | Barrier repair | pubmed_evidence | 1 | 0.693 |
| PANTHENOL | 덱스판테놀 | Hydrating | pubmed_evidence | 2 | 0.648 |
