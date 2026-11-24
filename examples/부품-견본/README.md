# 부품 견본

29개 구도를 모두 한 번씩 쓰는 수업 덱(세 막대로 삼각형 만들기)의 콘티와 그림 5장이다. 부품(`assets/kit/`)을 고친 뒤 디자인이 그대로인지 볼 때 쓴다. 정해진 구도 순서의 23·24번 장에는 그림 자리가 없어 `no-visual-repeat` 경고 1건을 그대로 둔다.
돌릴 때는 이 폴더를 `<임시폴더>`로 복사하고, 스킬 폴더에서 `python -X utf8 scripts/deck.py plan "<임시폴더>"`, `python -X utf8 scripts/deck.py scaffold "<임시폴더>"`, `python -X utf8 scripts/deck.py build "<임시폴더>"`, `python -X utf8 scripts/deck.py check "<임시폴더>" --shots`를 차례로 실행한다(산출물을 이 폴더에 남기지 않는다).
오류 0이어야 하고, `검사/sheet-NN.png`로 모양을 확인한다.
글자 한도까지 채운 경우는 `python -X utf8 tests/kit/stress.py --out <임시폴더>`(색 묶음 `--palette sky`, 어둡게 `--dark`)로 따로 시험한다.
