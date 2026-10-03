import QtQuick
import QtQuick.Effects

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
    property real pointerX: 0.5
    property real pointerY: 0.5
    property real reveal: 0.14

    SequentialAnimation on reveal {
        loops: Animation.Infinite
        NumberAnimation { from: 0.14; to: 0.93; duration: 2600; easing.type: Easing.InOutCubic }
        PauseAnimation { duration: 700 }
        NumberAnimation { from: 0.93; to: 0.14; duration: 2300; easing.type: Easing.InOutCubic }
        PauseAnimation { duration: 500 }
    }

    Item {
        id: scene
        anchors.fill: parent
        scale: hover.containsMouse ? 1.016 : 1.0
        x: (root.pointerX - 0.5) * -16
        y: (root.pointerY - 0.5) * -10
        Behavior on x { NumberAnimation { duration: 72; easing.type: Easing.OutCubic } }
        Behavior on y { NumberAnimation { duration: 72; easing.type: Easing.OutCubic } }
        Behavior on scale { NumberAnimation { duration: 95; easing.type: Easing.OutCubic } }

        Image {
            id: blurSource
            anchors.fill: parent
            source: root.heroSource
            fillMode: Image.PreserveAspectCrop
            smooth: true
            mipmap: true
            opacity: 0.34
        }

        MultiEffect {
            anchors.fill: parent
            source: blurSource
            blurEnabled: true
            autoPaddingEnabled: false
            blur: 0.72
            blurMax: 56
            saturation: -0.58
            brightness: -0.32
            contrast: 0.12
            opacity: 0.92
        }

        Item {
            anchors.top: parent.top
            anchors.bottom: parent.bottom
            width: Math.max(1, parent.width * root.reveal)
            clip: true
            Image {
                width: scene.width
                height: scene.height
                source: root.heroSource
                fillMode: Image.PreserveAspectCrop
                smooth: true
                mipmap: true
                opacity: 0.22
            }
        }
    }

    Rectangle {
        anchors.fill: parent
        gradient: Gradient {
            orientation: Gradient.Horizontal
            GradientStop { position: 0.0; color: "#F0020202" }
            GradientStop { position: 0.52; color: "#B8060607" }
            GradientStop { position: 1.0; color: "#F5020202" }
        }
    }

    Rectangle {
        id: aura
        width: 420
        height: 420
        radius: 210
        y: -180
        opacity: 0.055
        color: root.frostColor
        x: -220
        SequentialAnimation on x {
            loops: Animation.Infinite
            NumberAnimation { from: -220; to: root.width - 120; duration: 7600; easing.type: Easing.InOutSine }
            NumberAnimation { from: root.width - 120; to: -220; duration: 7600; easing.type: Easing.InOutSine }
        }
        layer.enabled: true
        layer.effect: MultiEffect {
            blurEnabled: true
            autoPaddingEnabled: false
            blur: 1.0
            blurMax: 72
        }
    }

    Repeater {
        model: 22
        Rectangle {
            required property int index
            property real originY: (index * 47) % Math.max(1, root.height)
            property real drift: 14 + (index % 5) * 4
            width: 1 + (index % 2)
            height: width
            radius: width / 2
            color: index % 6 === 0 ? root.frostColor : "#B9BDC2"
            opacity: 0.055 + (index % 4) * 0.018
            x: (index * 83) % Math.max(1, root.width)
            y: originY
            SequentialAnimation on y {
                loops: Animation.Infinite
                NumberAnimation { from: originY; to: originY - drift; duration: 2200 + index * 90; easing.type: Easing.InOutSine }
                NumberAnimation { from: originY - drift; to: originY; duration: 2200 + index * 90; easing.type: Easing.InOutSine }
            }
        }
    }

    Rectangle {
        anchors.fill: parent
        color: "transparent"
        border.color: "#3A3B3F"
        border.width: 1
        radius: root.radius
    }

    MouseArea {
        id: hover
        anchors.fill: parent
        hoverEnabled: true
        acceptedButtons: Qt.NoButton
        onPositionChanged: function(mouse) {
            root.pointerX = mouse.x / Math.max(1, width)
            root.pointerY = mouse.y / Math.max(1, height)
        }
        onExited: {
            root.pointerX = 0.5
            root.pointerY = 0.5
        }
    }

    Column {
        anchors.left: parent.left
        anchors.leftMargin: 36
        anchors.verticalCenter: parent.verticalCenter
        spacing: 8
        Text {
            text: "Q3ELITE  /  THEMEHUB GPU PROTOTYPE"
            color: "#8D9095"
            font.pixelSize: 11
            font.weight: Font.DemiBold
            font.letterSpacing: 0.8
        }
        Text {
            text: "MINIMAL BLACK.\nCINEMATIC DEPTH."
            color: "#F3F3F3"
            font.pixelSize: 31
            font.weight: Font.Bold
            lineHeight: 0.91
        }
        Text {
            text: "Move the cursor — parallax · GPU blur · reveal mask · particles · eased scale"
            color: "#B4B7BB"
            font.pixelSize: 12
        }
        Rectangle {
            width: 180
            height: 44
            radius: 10
            color: actionMouse.containsMouse ? "#FFFFFF" : "#DDE0E3"
            scale: actionMouse.pressed ? 0.985 : (actionMouse.containsMouse ? 1.022 : 1.0)
            Behavior on scale { NumberAnimation { duration: 62; easing.type: Easing.OutCubic } }
            Behavior on color { ColorAnimation { duration: 62 } }
            Text {
                anchors.centerIn: parent
                text: "PREVIEW EFFECT"
                color: "#070707"
                font.pixelSize: 13
                font.weight: Font.Bold
            }
            MouseArea {
                id: actionMouse
                anchors.fill: parent
                hoverEnabled: true
                onClicked: root.reveal = 0.14
            }
        }
    }

    Text {
        anchors.right: parent.right
        anchors.rightMargin: 22
        anchors.bottom: parent.bottom
        anchors.bottomMargin: 18
        text: "QT QUICK / GPU"
        color: "#B8BBC0"
        opacity: 0.66
        font.pixelSize: 10
        font.weight: Font.DemiBold
        font.letterSpacing: 0.7
    }
}
