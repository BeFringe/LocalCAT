# Synthetic fixture: comments may contain "quotes" and # marks.
translate zh_Hans intro_aa1:
    # guide thinking "The lantern glows." with dissolve
    voice "audio/synthetic_lantern.ogg"
    guide -thinking @ happy "灯笼亮着。" with fade
    nvl clear

translate zh_Hans strings:
    # "#" is a quoted hash inside a comment, not a slot.
    old "Open"
    new ""
    old "Open{#verb}"
    new "开启{#verb}"

translate zh_Hans narrator_bb2:
    # "The bridge is quiet."
    "桥上很安静。"

translate zh_Hans strings:
    old "Close"
    new "关闭"

translate zh_Hans outro_cc3:
    # guide "The lantern glows."
    guide ""
