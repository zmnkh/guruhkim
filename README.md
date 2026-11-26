# guruhkim

## docclass — 기밀/일반 문서 분류기

기밀 문서와 일반 문서를 각각 등록해 두고, 새 문서가 **기밀 문서에만 있는 내용**을 담고 있을 때만 기밀로 분류한다. 기밀 문서와 겹치더라도 일반 문서에 같은 내용이 있다는 근거를 댈 수 있으면 일반으로 분류한다.

### 판정 흐름

문서를 문장 단위로 나누고(표는 행 단위), 문장마다 아래 순서로 판정한다.

```
문장
 │
 ├─ 1. 일반 저장소와 해시 일치? ──────────── 예 → 일반
 │
 ├─ 2. 기밀 후보인가?
 │     · 기밀 문서에만 있는 문장과 해시 일치
 │     · 또는 기밀 문서와 의미상 유사 ───── 아니오 → 일반 (기밀과 무관)
 │
 └─ 3. 재검토: "이 문장이 왜 기밀이 아닌가?"
       LLM이 일반 문서(원본 등급)에서 근거를 인용
       │
       ├─ 근거가 검증을 통과 → 일반 + 4. 학습 등급에 추가
       └─ 근거 없음 / 검증 실패 → 기밀 + 사람 검토 대기열
```

재검토 후에도 기밀로 남은 문장이 하나라도 있으면 문서 전체가 기밀이다.

### 재검토의 근거 검증

LLM은 근거가 없어도 그럴듯한 이유를 만들 수 있으므로, 답을 그대로 믿지 않고 코드로 확인한다. 하나라도 어긋나면 기밀로 남긴다.

| 검사 | 막는 것 |
|---|---|
| 근거가 저장소의 원본 등급 일반 문장인가 | 지어낸 근거, 신규 문서 자신을 근거로 대는 것 |
| 인용문이 그 문장에 그대로 있는가 | 내용을 바꿔서 인용하는 것 |
| 근거 문장이 검토 문장과 충분히 비슷한가 | 엉뚱한 문장을 근거로 대는 것 |
| 검토 문장의 수치가 근거 문장에 모두 있는가 | "원가 4만 2천 원"을 "원가 4만 5천 원"으로 풀어 주는 것 |

LLM을 부르지 못하면(서버 장애 등) 재검토 대상 문장은 모두 기밀로 남긴다.

### 일반 저장소의 두 등급

| 등급 | 들어가는 것 | 해시 일치로 일반 판정 | 재검토의 근거 |
|---|---|---|---|
| 원본 | 사람이 등록한 일반 문서, 사람이 승인한 문장 | 예 | 예 |
| 학습 | 재검토에서 일반으로 풀린 문장 (자동) | 예 | **아니오** |

학습 문장을 근거로 쓰지 않는 이유는, 그렇게 하면 "비슷한 것의 비슷한 것"이 계속 풀리면서 원본에서 점점 멀어지기 때문이다. 학습 문장은 같은 문장이 다시 왔을 때 LLM 없이 바로 통과시키는 데만 쓴다.

학습 문장마다 근거 문서·근거 문장·처음 나온 문서·판정기·시각을 기록하며, 다음 경우 자동으로 무효가 된다.

- 근거가 된 일반 문서를 삭제하거나 다시 등록할 때
- 새로 등록한 기밀 문서에 같은 문장이 있을 때 (다음 분류 때 다시 재검토를 받는다)

### 설치

```bash
pip install -e '.[gpu,docling,docs]'  # GPU 서버(H200 등): 임베딩 모델 + Docling + 내장 추출기
pip install -e '.[docs]'              # 모델 없이 hashing 임베딩과 내장 추출기로만 시험할 때
```

### 문서 추출

모든 문서는 먼저 Markdown으로 변환한 뒤 임베딩한다. 기본 추출기는 [Docling](https://github.com/docling-project/docling)이고, Docling이 다루지 않는 형식은 내장 추출기가 맡는다.

| 형식 | 기본 추출기 | 비고 |
|---|---|---|
| `.pdf` | Docling | 레이아웃 분석, 표 구조 인식, 스캔본 OCR |
| `.docx` `.xlsx` `.xlsm` `.pptx` | Docling | PPTX 발표자 노트는 따로 읽어 덧붙인다 |
| `.png` `.jpg` `.tif` 등 이미지 | Docling | OCR |
| `.doc` `.xls` `.ppt` `.html` | Docling | 구형 Office 형식은 LibreOffice가 설치되어 있어야 한다 |
| `.hwp` `.hwpx` | 내장 | Docling 미지원 |
| `.txt` `.md` `.csv` | 내장 | UTF-8, CP949 |

`--extractor`로 바꿀 수 있다.

| 값 | 동작 |
|---|---|
| `auto` (기본) | Docling 우선. Docling이 없거나 실패하면 내장 추출기로 다시 읽고 경고한다 |
| `docling` | Docling만 쓴다. 실패하면 그 파일은 건너뛴다 |
| `builtin` | 내장 추출기만 쓴다 (PDF, DOCX, XLSX, PPTX, HWP, HWPX) |

변환 결과는 `convert`로 직접 확인할 수 있다.

```bash
python -m docclass convert 원가계획.xlsx            # 화면에 출력
python -m docclass convert -o md/ 문서/*            # md/<파일명>.md로 저장
python -m docclass --extractor builtin convert 보고서.pdf
```

Docling은 PDF와 이미지를 처음 처리할 때 레이아웃·OCR 모델을 Hugging Face에서 내려받는다. 인터넷이 막힌 서버에서는 다른 곳에서 `docling-tools models download`로 받아 옮긴 뒤 `DOCLING_ARTIFACTS_PATH`로 위치를 지정한다. Office 문서는 모델 없이 변환된다.

### 표 처리

표는 문장으로 나눌 수 없으므로 **행 하나를 한 단위**로 임베딩한다. 행만 떼어 놓아도 뜻이 통하도록 열 이름을 값 앞에 붙인다.

```
| 제품명 | 목표 원가 | 목표 판매가 |      제품명: 오로라 허브; 목표 원가: 42000; 목표 판매가: 129000
| --- | --- | --- |                 →   제품명: 오로라 센서; 목표 원가: 8500; 목표 판매가: 24900
| 오로라 허브 | 42000 | 129000 |
| 오로라 센서 | 8500 | 24900 |
```

- 2열짜리 표는 항목-값 표로 보고 행마다 `항목: 값`으로 만든다.
- 열이 많아 한 행이 길면 여러 단위로 나누고, 각 단위에 첫 열(행의 키)을 반복한다.
- 세로로 병합된 셀은 아래 행에도 같은 값을 채운다.

### 사용법

```bash
# 1. 등록 (처음 등록할 때 고른 임베딩이 저장소에 기록된다)
python -m docclass --embedder bge-m3 register --label confidential 기밀문서/*
python -m docclass register --label general 일반문서/*

# 2. 재검토에 쓸 로컬 LLM 지정 (OpenAI 호환 API: vLLM, Ollama 등)
export DOCCLASS_LLM_URL=http://localhost:8000/v1
export DOCCLASS_LLM_MODEL=<서버에 띄운 모델 이름>

# 3. 분류
python -m docclass classify 신규문서/*            # 요약
python -m docclass classify -v 신규문서/*         # 모든 문장 표시
python -m docclass classify --json 신규문서/*     # 판정 근거 전체

# 4. 사람 검토: 근거가 없어 기밀로 남은 문장 (자주 나온 순)
python -m docclass pending
python -m docclass approve <ID> --by 홍길동       # 일반으로 승인 → 원본 등급
python -m docclass dismiss <ID>                   # 기밀이 맞음

# 5. 표본 점검: 자동으로 일반 처리된 문장과 그 근거
python -m docclass learned --sample 20
python -m docclass revoke <ID>                    # 잘못 풀린 문장 취소

# 관리
python -m docclass list
python -m docclass remove 일반문서/회사소개.docx
```

GPU나 모델 없이 시험하려면 `--embedder hashing --store demo_store`를 쓴다. LLM 설정이 없으면 재검토는 임베딩 유사도만으로 하고(`similarity`), 이때는 학습을 기본으로 끈다. 유사도는 수치 차이에 둔해서 그 결과를 굳히면 위험하기 때문이다.

| 옵션 | 기본값 | 설명 |
|---|---|---|
| `--store` | `store` | 인덱스 디렉터리 |
| `--embedder` | `bge-m3` | `hashing` 또는 sentence-transformers 모델 이름 |
| `--device` | 자동 | `cuda`, `cuda:0`, `cpu` |
| `--extractor` | `auto` | `docling`, `builtin` |
| `--judge` | `auto` | `llm`, `similarity`. `auto`는 LLM 설정이 있으면 `llm` |
| `--llm-url`, `--llm-model` | 환경 변수 | 재검토 LLM. API 키가 필요하면 `DOCCLASS_LLM_API_KEY` |
| `classify --learn` / `--no-learn` | LLM일 때 켬 | 풀린 문장을 학습 등급에 추가 |
| `classify --t-important` | 0.80 | 기밀 후보가 되는 유사도 |
| `classify --t-evidence` | 0.60 | 근거 문장으로 인정하는 최소 유사도 |
| `classify --t-general`, `--t-margin` | 0.80, 0.05 | `similarity` 재검토 기준 |
| `classify --t-min-hits` | 1 | 문서를 기밀로 판정하는 기밀 문장 수 |

### 한계와 주의

- 재검토 품질은 LLM에 달려 있다. LLM이 놓친 근거는 기밀로 남고(안전한 쪽), LLM이 잘못 댄 근거는 검증에서 대부분 걸러지지만, 수치가 없고 표현이 비슷한 문장은 검증만으로 구분하지 못한다. `learned --sample`로 주기적으로 확인해야 한다.
- 인사말이나 상투 문구처럼 기밀 문서에만 있고 일반 문서에는 없는 문장은 근거가 없어 기밀로 남는다. `pending`에서 승인하거나 공통 문구를 일반 문서로 등록해 해결한다.
- 일반 문서 등록과 `approve`는 해당 내용을 기밀에서 해제하는 것과 같다. 운영 시에는 이 명령의 권한을 따로 관리해야 한다.

- 등록과 분류에는 같은 추출기를 써야 한다. 추출기가 다르면 같은 문서도 Markdown이 조금 달라져 정확 일치가 줄어든다. `auto`가 내장 추출기로 대신 읽었다는 경고가 나오면 원인을 먼저 해결하는 편이 좋다.
- 한국어 OCR은 EasyOCR이 설치되어 있을 때 켜진다(`docling` 설치 옵션에 포함). 없으면 Docling 기본 OCR을 쓰는데 한국어 인식이 보장되지 않는다.
- 내장 추출기는 OCR이 없어 스캔한 PDF를 읽지 못한다. 암호가 걸린 문서와 배포용 HWP는 어느 쪽으로도 읽지 못한다. 읽지 못한 파일은 건너뛰고 경고를 낸다.
- Docling으로 뽑은 XLSX에는 시트 이름이, PPTX에는 슬라이드 번호가 들어가지 않는다.
- 표의 첫 행을 머리글로 본다. 머리글이 여러 줄이거나 가로로 병합된 표는 열 이름이 어색하게 붙을 수 있다.
- 엑셀 수식은 엑셀이 마지막으로 저장한 계산값을 읽는다. 엑셀에서 한 번도 열지 않은 파일은 수식 칸이 비어 나온다.
- PDF는 줄바꿈 위치를 추정해 문장을 잇기 때문에 원본과 띄어쓰기가 달라질 수 있다. 이 경우 정확 일치 대신 의미 유사도로 잡힌다.
- 같은 표라도 열 구성이 달라지면 정확 일치는 안 되고 의미 유사도에 의존한다.
- 임계값(0.80, 0.60)은 출발점일 뿐이다. 실제 문서로 검증 세트를 만들어 조정해야 한다.
- `hashing` 임베딩은 글자 겹침만 보므로 표현을 많이 바꾼 문장은 놓친다. 데모와 테스트 전용이다.
- 20자 미만 문장은 이웃 문장에 붙이거나 버린다. 아주 짧은 기밀 값 하나만 옮긴 경우는 잡지 못한다.
- 저장소는 numpy 파일이다. 문서가 많아지면 `Store`를 벡터 DB로 교체한다.

### 테스트

```bash
pip install -e '.[dev,docs]' && pytest   # docling이 설치되어 있으면 Docling 경로도 함께 검사
```
