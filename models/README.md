# Local object detection model

`yolox_s_int8.onnx` is the OpenCV Zoo YOLOX-s INT8 model from
[opencv/opencv_zoo](https://github.com/opencv/opencv_zoo/tree/main/models/object_detection_yolox).
The model and its upstream implementation are licensed under Apache-2.0.
See the [upstream license](https://github.com/opencv/opencv_zoo/blob/main/models/object_detection_yolox/LICENSE)
before redistributing this directory.

SHA-256 of the model downloaded on 2026-09-26:
`01a3b0f400b30bc1e45230e991b2e499ab42622485a330021947333fbaf03935`.

The application uses only COCO ground-level classes (person, bicycle, car,
motorcycle, bus, truck) for motion filtering. Neither
the model nor the analysis uploads videos to a server.
