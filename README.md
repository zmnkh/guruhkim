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
pip install -e '.[gpu]'   # GPU 서버(H200 등): sentence-transformers 포함
pip install -e .          # 모델 없이 hashing 임베딩으로만 시험할 때
```

### 사용법

```bash
# 등록 (처음 등록할 때 고른 임베딩이 저장소에 기록된다)
python -m docclass --embedder bge-m3 register --label important examples/important/*.txt
python -m docclass register --label general examples/general/*.txt

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

- 입력은 UTF-8 텍스트 파일이다. PDF, DOCX, HWP는 먼저 텍스트로 추출해야 한다.
- bge-m3 임계값(0.80)은 출발점일 뿐이다. 실제 문서로 검증 세트를 만들어 조정해야 한다.
- `hashing` 임베딩은 글자 겹침만 보므로 표현을 많이 바꾼 문장은 놓친다. 데모와 테스트 전용이다.
- 20자 미만 문장은 이웃 문장에 붙이거나 버린다. 아주 짧은 기밀 값 하나만 옮긴 경우는 잡지 못한다.
- 일반 문서 등록은 해당 내용을 기밀에서 해제하는 것과 같다. 운영 시에는 등록 권한과 감사 로그를 따로 둬야 한다.
- 저장소는 numpy 파일이다. 문서가 많아지면 `Store`를 벡터 DB로 교체한다.

### 테스트

```bash
pip install -e '.[dev]' && pytest
```
