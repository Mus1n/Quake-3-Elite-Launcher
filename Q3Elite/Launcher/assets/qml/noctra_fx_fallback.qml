import QtQuick

Rectangle {
    id: root
    width: 1100
    height: 380
    radius: 16
    clip: true
    color: "#020202"
    property url heroSource: ""
    property color accentColor: "#E2E5E8"
    property color frostColor: "#FFFFFF"

    Image {
        anchors.fill: parent
        source: root.heroSource
        fillMode: Image.PreserveAspectCrop
        smooth: true
        opacity: 0.15
        scale: 1.04
    }

    Rectangle {
        anchors.fill: parent
        color: "#C8000000"
    }

    Rectangle {
        id: glow
        width: 260
        height: root.height
        color: "#0DFFFFFF"
        x: -260
        SequentialAnimation on x {
            loops: Animation.Infinite
            NumberAnimation {
                from: -260
                to: root.width
                duration: 6000
                easing.type: Easing.InOutSine
            }
            NumberAnimation {
                from: root.width
                to: -260
                duration: 6000
                easing.type: Easing.InOutSine
            }
        }
    }

    Column {
        anchors.left: parent.left
        anchors.leftMargin: 36
        anchors.verticalCenter: parent.verticalCenter
        spacing: 8
        Text { text: "Q3ELITE  /  QT QUICK FALLBACK"; color: "#8D9095"; font.pixelSize: 11; font.weight: Font.DemiBold }
        Text { text: "THEMEHUB\nMOTION PROTOTYPE"; color: "#F3F3F3"; font.pixelSize: 31; font.weight: Font.Bold }
        Text { text: "Fallback scene loaded without QtQuick.Effects."; color: "#B4B7BB"; font.pixelSize: 12 }
    }

    Rectangle {
        anchors.fill: parent
        color: "transparent"
        border.color: "#3A3B3F"
        border.width: 1
        radius: root.radius
    }
}
