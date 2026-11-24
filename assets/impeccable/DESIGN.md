---
name: "스티커 팝"
description: "교실 화면을 위한 큰 글자와 스티커 한 벌"
colors:
  ground: "#FFF8EA"
  ink: "#232B5C"
  accent: "#EF5350"
  coral: "#FF6B6B"
  yellow: "#FFD166"
  mint: "#2EC4B6"
  sky: "#4DABF7"
  violet: "#9D8CFF"
  card: "#FFFFFF"
rounded:
  r: "40px"
  r-m: "30px"
  r-s: "18px"
---
# Design System: 스티커 팝
## Overview
`{{title}}`의 색 묶음은 `{{palette}}`다. 밝은 점 바탕, 남색 외곽선과 그림자, 기울어진 단계 표시, 같은 마스코트의 포즈를 한 벌로 쓴다.
## Colors
위 값은 기본 `sunny`다. 다른 색 묶음은 부품의 `palettes.json`과 CSS를 따른다. 맞고 틀림은 색뿐 아니라 기호와 모양으로 나타낸다.
## Typography
제목·본문은 Maplestory 700·300, fontTools와 Noto Sans KR이 있으면 없는 글자를 Deck Sans로 보충한다. 1920×1080 기준 본문 46px, 작은 글자 토큰 32px; 수식은 Cambria Math.
## Layout
한 장에 한 초점. 제목 → 핵심 → 보조 순으로 읽고 구도는 등록부에서 고른다. 넘치면 내용을 덜거나 장을 나눈다.
## Elevation & Depth
카드의 딱딱한 그림자는 `8px 8px 0 var(--line)`, 작은 그림자는 `5px 5px 0 var(--line)`이다.
## Shapes
흰 카드와 단추에는 굵은 남색 선을 쓴다. 단계 표시는 기울어진 스티커처럼 둔다.
## Components
단계 표시는 찍히고 제목과 내용은 부드럽게 올라온다. 마스코트는 뛰어들고 말풍선은 튄다. 단계 공개와 답의 반응에만 움직임을 더한다.
## Do's and Don'ts
- 임페커블의 eyebrow 금지와 달리 `탐구 1` 같은 단계 표시는 수업 길잡이로 둔다.
- 오프셋 그림자는 스티커 팝의 일부다. 견주기 카드 2~4개는 같은 크기로 둔다.
- 캐릭터·사물은 생성 그림, 정확한 수학 도형·그래프는 SVG를 쓴다.
- 동작 줄이기 대안은 넣지 않는다. 화면 연출은 교실에서 늘 같게 둔다.
- 웹 본문 폭·16px 기준 대신 본문 기본 46px, 학생이 읽는 문장 32px 이상, 눈금·단위·쪽 번호 같은 보조 글자 26px 이상, 장당 한 메시지를 따른다. 검사는 26px 미만만 잡으므로 32px은 눈으로 확인한다.
- 옅은 점 바탕과 내용인 좌표평면·모눈은 유지한다.
