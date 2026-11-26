# guruhkim

## docclass — 중요/일반 문서 분류기

중요 문서와 일반 문서를 각각 등록해 두고, 새 문서가 **중요 문서에만 있는 내용**을 담고 있을 때만 중요 문서로 분류한다. 중요 문서에 들어 있더라도 일반 문서에도 있는 내용(공개 자료, 공통 문구 등)만 쓴 문서는 일반 문서가 된다.

### 판정 방식

문서를 문장 단위로 나누고, 문장마다 두 인덱스를 모두 조회한다.

| 중요 인덱스 | 일반 인덱스 | 문장 판정 |
|---|---|---|
| 중요 전용 문장과 정확히 일치 | — | 중요 (`exact`) |
| 유사도 높음 | 유사도 낮음 | 중요 (`semantic`) |
| 유사도 높음 | 비슷하게 높음 | 일반 (`neutralized`) |
| 유사도 낮음 | — | 일반 (`no_match`) |

중요 문장이 `min_hits`개(기본 1개) 이상이면 문서 전체를 중요로 분류한다. 판정은 조회 시점에 하므로 등록 순서는 상관없다.

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
# 등록 (처음 등록할 때 고른 임베딩이 저장소에 기록된다)
python -m docclass --embedder bge-m3 register --label important 중요문서/*.pdf 중요문서/*.xlsx
python -m docclass register --label general 일반문서/*.hwp 일반문서/*.pptx

# 분류
python -m docclass classify examples/incoming/*.txt        # 요약
python -m docclass classify -v examples/incoming/*.txt     # 모든 문장 표시
python -m docclass classify --json examples/incoming/*.txt # 판정 근거 전체

# 관리
python -m docclass list
python -m docclass remove examples/general/회사소개_및_시장자료.txt
```

GPU나 모델 없이 시험하려면 `--embedder hashing --store demo_store`를 쓴다.

| 옵션 | 기본값 | 설명 |
|---|---|---|
| `--store` | `store` | 인덱스 디렉터리 |
| `--embedder` | `bge-m3` | `hashing` 또는 sentence-transformers 모델 이름 |
| `--device` | 자동 | `cuda`, `cuda:0`, `cpu` |
| `--t-important` | 0.80 | 중요 후보가 되는 유사도 |
| `--t-general` | 0.80 | 상쇄 자격이 생기는 유사도 |
| `--t-margin` | 0.05 | 일반 유사도가 (중요 유사도 − margin) 이상이면 상쇄 |
| `--t-min-hits` | 1 | 문서를 중요로 판정하는 중요 문장 수 |

### 한계와 주의

- 등록과 분류에는 같은 추출기를 써야 한다. 추출기가 다르면 같은 문서도 Markdown이 조금 달라져 정확 일치가 줄어든다. `auto`가 내장 추출기로 대신 읽었다는 경고가 나오면 원인을 먼저 해결하는 편이 좋다.
- 한국어 OCR은 EasyOCR이 설치되어 있을 때 켜진다(`docling` 설치 옵션에 포함). 없으면 Docling 기본 OCR을 쓰는데 한국어 인식이 보장되지 않는다.
- 내장 추출기는 OCR이 없어 스캔한 PDF를 읽지 못한다. 암호가 걸린 문서와 배포용 HWP는 어느 쪽으로도 읽지 못한다. 읽지 못한 파일은 건너뛰고 경고를 낸다.
- Docling으로 뽑은 XLSX에는 시트 이름이, PPTX에는 슬라이드 번호가 들어가지 않는다.
- 표의 첫 행을 머리글로 본다. 머리글이 여러 줄이거나 가로로 병합된 표는 열 이름이 어색하게 붙을 수 있다.
- 엑셀 수식은 엑셀이 마지막으로 저장한 계산값을 읽는다. 엑셀에서 한 번도 열지 않은 파일은 수식 칸이 비어 나온다.
- PDF는 줄바꿈 위치를 추정해 문장을 잇기 때문에 원본과 띄어쓰기가 달라질 수 있다. 이 경우 정확 일치 대신 의미 유사도로 잡힌다.
- 같은 표라도 열 구성이 달라지면 정확 일치는 안 되고 의미 유사도에 의존한다.
- bge-m3 임계값(0.80)은 출발점일 뿐이다. 실제 문서로 검증 세트를 만들어 조정해야 한다.
- `hashing` 임베딩은 글자 겹침만 보므로 표현을 많이 바꾼 문장은 놓친다. 데모와 테스트 전용이다.
- 20자 미만 문장은 이웃 문장에 붙이거나 버린다. 아주 짧은 기밀 값 하나만 옮긴 경우는 잡지 못한다.
- 일반 문서 등록은 해당 내용을 기밀에서 해제하는 것과 같다. 운영 시에는 등록 권한과 감사 로그를 따로 둬야 한다.
- 저장소는 numpy 파일이다. 문서가 많아지면 `Store`를 벡터 DB로 교체한다.

### 테스트

```bash
pip install -e '.[dev,docs]' && pytest   # docling이 설치되어 있으면 Docling 경로도 함께 검사
```
