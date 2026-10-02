using System.ComponentModel;
using System.Text.Json;
using System.Text.Json.Serialization;
using BlenderMcp.Server.Bridge;
using Microsoft.Extensions.AI;
using ModelContextProtocol.Server;

namespace BlenderMcp.Server.Tools;

/// <summary>One keyframe for insert_keyframes.</summary>
public sealed record Keyframe(
    [property: JsonPropertyName("frame"), Description("Frame number (may be fractional).")] double Frame,
    [property: JsonPropertyName("location"), Description("[x, y, z] metres (bones: relative to their rest position).")] double[]? Location = null,
    [property: JsonPropertyName("rotation"), Description("[x, y, z] Euler degrees; converted to quaternion/axis-angle if that is the rotation mode.")] double[]? Rotation = null,
    [property: JsonPropertyName("scale"), Description("[x, y, z]")] double[]? Scale = null,
    [property: JsonPropertyName("properties"), Description("Any other animatable property by data path relative to the object/bone, e.g. {\"data.energy\": 500} on a light, {\"hide_render\": true}. Angles in degrees.")] Dictionary<string, JsonElement>? Properties = null);

/// <summary>A bone for create_armature.</summary>
public sealed record BoneSpec(
    [property: JsonPropertyName("name")] string Name,
    [property: JsonPropertyName("head"), Description("[x, y, z] armature-local metres.")] double[] Head,
    [property: JsonPropertyName("tail"), Description("[x, y, z] armature-local metres.")] double[] Tail,
    [property: JsonPropertyName("parent")] string? Parent = null,
    [property: JsonPropertyName("connect"), Description("Attach the head to the parent's tail (only if they touch).")] bool? Connect = null,
    [property: JsonPropertyName("roll"), Description("Roll in degrees.")] double? Roll = null,
    [property: JsonPropertyName("deform"), Description("Deform bone (default true); false for control/IK bones.")] bool? Deform = null);

/// <summary>A bone pose for pose_bones.</summary>
public sealed record BonePose(
    [property: JsonPropertyName("location")] double[]? Location = null,
    [property: JsonPropertyName("rotation"), Description("Euler degrees.")] double[]? Rotation = null,
    [property: JsonPropertyName("scale")] double[]? Scale = null);

/// <summary>Keyframes, timeline, presets, clips, previews.</summary>
[McpServerToolType]
public static class AnimationTools
{
    [McpServerTool(Name = "insert_keyframes", Title = "Insert keyframes")]
    [Description("Key an object or a pose bone at one or more frames in one call: location, rotation (degrees), scale and/or any animatable property. Example keys: [{frame:1, location:[0,0,0]}, {frame:24, location:[0,0,2], rotation:[0,0,90]}]. Pass action to key into a named clip (created if missing).")]
    public static async Task<string> InsertKeyframes(
        BlenderConnection blender,
        string @object,
        [Description("Keys to insert.")] Keyframe[] keys,
        [Description("Pose bone (armatures).")] string? bone = null,
        [Description("Action (clip) to key into; created and made active if missing.")] string? action = null,
        [Description("Interpolation for these keys: CONSTANT | LINEAR | BEZIER | SINE | QUAD | CUBIC | BACK | BOUNCE | ELASTIC ...")] string? interpolation = null,
        CancellationToken ct = default)
        => Results.Text(await blender.CallAsync("insert_keyframes", new Args
        {
            ["object"] = @object, ["keys"] = keys, ["bone"] = bone, ["action"] = action, ["interpolation"] = interpolation,
        }, ct: ct));

    [McpServerTool(Name = "delete_keyframes", Title = "Delete keyframes", Destructive = true)]
    [Description("Delete keys of an object or bone. Filter by channels (location | rotation | scale | a data path), frames or frameRange; with neither frames nor frameRange all keys of the matching channels are removed.")]
    public static async Task<string> DeleteKeyframes(
        BlenderConnection blender,
        string @object,
        string? bone = null,
        [Description("location | rotation | scale | data path (default: all).")] string[]? channels = null,
        double[]? frames = null,
        [Description("[first, last] inclusive.")] double[]? frameRange = null,
        CancellationToken ct = default)
        => Results.Text(await blender.CallAsync("delete_keyframes", new Args
        {
            ["object"] = @object, ["bone"] = bone, ["channels"] = channels, ["frames"] = frames, ["frame_range"] = frameRange,
        }, ct: ct));

    [McpServerTool(Name = "set_timeline", Title = "Set timeline")]
    [Description("Scene frame range, frame rate (e.g. 30 for Unreal) and current frame. fitToAction=<object> fits the range to that object's active action.")]
    public static async Task<string> SetTimeline(
        BlenderConnection blender,
        int? frameStart = null, int? frameEnd = null,
        [Description("Frames per second (29.97 etc. allowed).")] double? fps = null,
        [Description("Jump to this frame.")] int? frame = null,
        string? fitToAction = null,
        CancellationToken ct = default)
        => Results.Text(await blender.CallAsync("set_timeline", new Args
        {
            ["frame_start"] = frameStart, ["frame_end"] = frameEnd, ["fps"] = fps, ["frame"] = frame, ["fit_to_action"] = fitToAction,
        }, ct: ct));

    [McpServerTool(Name = "set_interpolation", Title = "Interpolation / looping")]
    [Description("Change interpolation and easing of existing keys, make the animation loop (Cycles modifier; loop='offset' keeps accumulating, e.g. endless spin), or set extrapolation. Filter by bone and channels.")]
    public static async Task<string> SetInterpolation(
        BlenderConnection blender,
        string @object,
        string? bone = null,
        [Description("location | rotation | scale | data path (default: all).")] string[]? channels = null,
        [Description("CONSTANT | LINEAR | BEZIER | SINE | QUAD | CUBIC | QUART | QUINT | EXPO | CIRC | BACK | BOUNCE | ELASTIC")] string? interpolation = null,
        [Description("AUTO | EASE_IN | EASE_OUT | EASE_IN_OUT (for the easing interpolations).")] string? easing = null,
        [Description("true = loop, 'offset' = loop with accumulating offset, false = stop looping.")] JsonElement? loop = null,
        [Description("CONSTANT | LINEAR")] string? extrapolation = null,
        CancellationToken ct = default)
        => Results.Text(await blender.CallAsync("set_interpolation", new Args
        {
            ["object"] = @object, ["bone"] = bone, ["channels"] = channels, ["interpolation"] = interpolation,
            ["easing"] = easing, ["loop"] = loop, ["extrapolation"] = extrapolation,
        }, ct: ct));

    [McpServerTool(Name = "animate_preset", Title = "Procedural animation preset")]
    [Description("One-call motion for props and pickups: spin (amount = turns), bob (amount = metres), sway (amount = degrees), pulse (amount = scale fraction), bounce (amount = metres), follow_path (path = curve). Loops by default. Works on objects or pose bones. Pass action to make it a named clip.")]
    public static async Task<string> AnimatePreset(
        BlenderConnection blender,
        string @object,
        [Description("spin | bob | sway | pulse | bounce | follow_path")] string preset,
        [Description("X | Y | Z (default Z).")] string? axis = null,
        [Description("Strength; meaning depends on the preset.")] double? amount = null,
        [Description("Length of one cycle in frames (default 2 seconds).")] double? period = null,
        [Description("First frame (default scene start).")] double? start = null,
        bool? loop = null,
        string? bone = null,
        [Description("Action (clip) name to put the keys in.")] string? action = null,
        [Description("follow_path: curve object.")] string? path = null,
        [Description("follow_path: TRACK_NEGATIVE_Y (default, Blender front) | FORWARD_X | FORWARD_Y | ...")] string? forwardAxis = null,
        CancellationToken ct = default)
        => Results.Text(await blender.CallAsync("animate_preset", new Args
        {
            ["object"] = @object, ["preset"] = preset, ["axis"] = axis, ["amount"] = amount, ["period"] = period,
            ["start"] = start, ["loop"] = loop, ["bone"] = bone, ["action"] = action, ["path"] = path,
            ["forward_axis"] = forwardAxis,
        }, ct: ct));

    [McpServerTool(Name = "list_actions", ReadOnly = true, Title = "List actions (clips)")]
    [Description("All actions (animation clips) in the file: frame range, length in seconds, keys, bones animated, which objects use them, looping, fake user.")]
    public static async Task<string> ListActions(BlenderConnection blender, CancellationToken ct = default)
        => Results.Text(await blender.CallAsync("list_actions", new Args(), ct: ct));

    [McpServerTool(Name = "get_animation", ReadOnly = true, Title = "Get animation")]
    [Description("An object's animation: active action and slot, keyed channels per object/bone (key counts, ranges, interpolation, looping), NLA tracks, timeline, and for armatures the root/hips travel (root motion). includeKeys adds key values.")]
    public static async Task<string> GetAnimation(
        BlenderConnection blender,
        string @object,
        [Description("Only this bone's channels.")] string? bone = null,
        bool? includeKeys = null,
        [Description("Cap on key values returned (default 200).")] int? maxKeys = null,
        CancellationToken ct = default)
        => Results.Text(await blender.CallAsync("get_animation", new Args
        {
            ["object"] = @object, ["bone"] = bone, ["include_keys"] = includeKeys, ["max_keys"] = maxKeys,
        }, ct: ct));

    [McpServerTool(Name = "set_action", Title = "Set active action (clip)")]
    [Description("Make an action the object's active clip (created if missing), so new keys go into it and it plays. Optionally set a manual frame range and looping. Actions get a fake user so inactive clips are kept on save. Fits the timeline to the clip by default.")]
    public static async Task<string> SetAction(
        BlenderConnection blender,
        string @object,
        string action,
        [Description("Create the action if it doesn't exist (default true).")] bool? create = null,
        [Description("Manual clip range [start, end].")] double[]? frameRange = null,
        bool? loop = null,
        [Description("Set the scene range to the clip (default true).")] bool? fitTimeline = null,
        CancellationToken ct = default)
        => Results.Text(await blender.CallAsync("set_action", new Args
        {
            ["object"] = @object, ["action"] = action, ["create"] = create, ["frame_range"] = frameRange,
            ["loop"] = loop, ["fit_timeline"] = fitTimeline,
        }, ct: ct));

    [McpServerTool(Name = "manage_action", Title = "Rename / duplicate / delete action")]
    [Description("rename, duplicate (e.g. start a Run from a Walk) or delete an action.")]
    public static async Task<string> ManageAction(
        BlenderConnection blender,
        string action,
        [Description("rename | duplicate | delete")] string operation,
        string? newName = null,
        CancellationToken ct = default)
        => Results.Text(await blender.CallAsync("manage_action", new Args
        {
            ["action"] = action, ["operation"] = operation, ["new_name"] = newName,
        }, ct: ct));

    [McpServerTool(Name = "render_animation_frames", ReadOnly = true, Title = "Animation contact sheet")]
    [Description("See motion: renders N evenly spaced frames of the animation into ONE contact-sheet image, from a fixed camera that frames the whole movement (so travel is visible). Pass the animated objects, or an armature to render its skinned meshes.")]
    public static async Task<IEnumerable<AIContent>> RenderAnimationFrames(
        BlenderConnection blender,
        string[] objects,
        [Description("[first, last] (default: the action's range).")] double[]? frameRange = null,
        [Description("Frames to show, 1-24 (default 8).")] int? count = null,
        [Description("front | back | left | right | top | iso ... or [azimuth, elevation] degrees.")] JsonElement? view = null,
        [Description("Tile size px (default 256).")] int? size = null,
        int? columns = null,
        [Description("SOLID | MATERIAL | RENDERED | WIREFRAME")] string? shading = null,
        bool? wireframe = null,
        [Description("Hide everything else (default true).")] bool? isolate = null,
        CancellationToken ct = default)
    {
        var img = await blender.CallAsync("render_animation_frames", new Args
        {
            ["objects"] = objects, ["frame_range"] = frameRange, ["count"] = count, ["view"] = view, ["size"] = size,
            ["columns"] = columns, ["shading"] = shading, ["wireframe"] = wireframe, ["isolate"] = isolate,
        }, TimeSpan.FromMinutes(2), ct);
        return Results.ImageWithCaption(img, $"Frames {img?["frames"]?.ToJsonString()} at {img?["fps"]} fps, left to right, top to bottom");
    }
}

/// <summary>Armatures, skinning, posing, shape keys.</summary>
[McpServerToolType]
public static class RigTools
{
    [McpServerTool(Name = "create_armature", Title = "Create armature")]
    [Description("Create an armature from a bone list and/or template='humanoid' (Unreal mannequin bone names: root, pelvis, spine_01-03, neck_01, head, clavicle/upperarm/lowerarm/hand, thigh/calf/foot/ball _l/_r; facing -Y, T-pose, scaled to height).")]
    public static async Task<string> CreateArmature(
        BlenderConnection blender,
        [Description("Object name (default Armature_Rig).")] string? name = null,
        [Description("humanoid")] string? template = null,
        [Description("Template height in metres (default 1.8).")] double? height = null,
        [Description("Bones to add.")] BoneSpec[]? bones = null,
        double[]? location = null,
        string? collection = null,
        [Description("OCTAHEDRAL | STICK | BBONE | ENVELOPE | WIRE")] string? display = null,
        [Description("Draw bones in front of meshes (default true).")] bool? inFront = null,
        CancellationToken ct = default)
        => Results.Text(await blender.CallAsync("create_armature", new Args
        {
            ["name"] = name, ["template"] = template, ["height"] = height, ["bones"] = bones, ["location"] = location,
            ["collection"] = collection, ["display"] = display, ["in_front"] = inFront,
        }, ct: ct));

    [McpServerTool(Name = "parent_with_weights", Title = "Skin mesh to armature")]
    [Description("Bind meshes to an armature: mode=auto (automatic heat weights, for organic meshes), envelope, empty (groups only, for painting), or rigid (every vertex 100% on one bone - ideal for hard-surface parts like doors, turrets, drone rotors). Vertices bone heat misses (common on intersecting or very low-poly parts) are bound to their nearest bone and reported.")]
    public static async Task<string> ParentWithWeights(
        BlenderConnection blender,
        string[] objects,
        string armature,
        [Description("auto | envelope | empty | rigid")] string? mode = null,
        [Description("rigid: the bone to bind to.")] string? bone = null,
        [Description("auto/envelope: bind vertices the weighting missed to their nearest bone (default true).")] bool? fillUnweighted = null,
        CancellationToken ct = default)
        => Results.Text(await blender.CallAsync("parent_with_weights", new Args
        {
            ["objects"] = objects, ["armature"] = armature, ["mode"] = mode, ["bone"] = bone, ["fill_unweighted"] = fillUnweighted,
        }, TimeSpan.FromMinutes(5), ct));

    [McpServerTool(Name = "pose_bones", Title = "Pose bones")]
    [Description("Pose several bones at once, e.g. {\"upperarm_l\":{\"rotation\":[0,0,-60]}, \"head\":{\"rotation\":[10,0,0]}}. Rotations in degrees, location relative to rest. With frame, the pose is keyed there (into action if given). reset=true returns all bones to rest first.")]
    public static async Task<string> PoseBones(
        BlenderConnection blender,
        [Description("Armature (or a mesh skinned to it).")] string armature,
        Dictionary<string, BonePose> pose,
        double? frame = null,
        [Description("Insert keys (default: true when frame is given).")] bool? key = null,
        string? action = null,
        bool? reset = null,
        CancellationToken ct = default)
        => Results.Text(await blender.CallAsync("pose_bones", new Args
        {
            ["armature"] = armature, ["pose"] = pose, ["frame"] = frame, ["key"] = key, ["action"] = action, ["reset"] = reset,
        }, ct: ct));

    [McpServerTool(Name = "set_shape_key", Title = "Set shape key")]
    [Description("Set a shape key (morph target) value and optionally key it at a frame. Creates the key (and Basis) if missing - a new key starts identical to the base shape.")]
    public static async Task<string> SetShapeKey(
        BlenderConnection blender,
        string @object,
        string name,
        [Description("0..1")] double? value = null,
        double? frame = null,
        bool? create = null,
        [Description("New key from the current mix of keys.")] bool? fromMix = null,
        CancellationToken ct = default)
        => Results.Text(await blender.CallAsync("set_shape_key", new Args
        {
            ["object"] = @object, ["name"] = name, ["value"] = value, ["frame"] = frame, ["create"] = create, ["from_mix"] = fromMix,
        }, ct: ct));
}

/// <summary>Skeletal mesh + animation round trip with Unreal.</summary>
[McpServerToolType]
public static class UnrealAnimationTools
{
    [McpServerTool(Name = "validate_rig", ReadOnly = true, Title = "Validate rig for Unreal")]
    [Description("Pre-flight check before taking a character to Unreal: single root bone, armature transforms applied, extra-root-bone naming, leaf/end bones, bone names, unweighted vertices, >8 influences, unparented meshes, scene fps vs target, unit scale, actions (bones they animate, fake users). Returns ok plus issues with fixes.")]
    public static async Task<string> ValidateRig(
        BlenderConnection blender,
        [Description("Armature (or a mesh skinned to it).")] string armature,
        [Description("Unreal project frame rate (default 30).")] double? targetFps = null,
        [Description("Only deform bones are exported (default true).")] bool? deformOnly = null,
        CancellationToken ct = default)
        => Results.Text(await blender.CallAsync("validate_rig", new Args
        {
            ["armature"] = armature, ["target_fps"] = targetFps, ["deform_only"] = deformOnly,
        }, ct: ct));

    [McpServerTool(Name = "export_animation_for_unreal", Title = "Export character for Unreal")]
    [Description("Export a rig for Unreal: SK_<asset>.fbx (skinned meshes + skeleton in bind pose) and one A_<asset>_<clip>.fbx per action, with Unreal-safe settings (no extra root bone, no leaf bones, deform bones only, baked keys over each clip's own range, written in centimetres so the root bone has scale 1). Runs validate_rig first (errors stop the export unless force=true). Returns the files, root-motion hints and a ready-to-run Unreal Python import script.")]
    public static async Task<string> ExportAnimationForUnreal(
        BlenderConnection blender,
        [Description("Armature (or a mesh skinned to it).")] string armature,
        [Description("Asset base name (default from the armature).")] string? assetName = null,
        [Description("Output folder (default: a temp folder).")] string? folder = null,
        [Description("Actions to export (default: every action that animates this rig).")] string[]? actions = null,
        [Description("Also write the skeletal mesh file (default true).")] bool? includeMesh = null,
        bool? deformOnly = null,
        [Description("Unreal content folder for the import script (default /Game/Characters/<asset>).")] string? unrealFolder = null,
        double? targetFps = null,
        [Description("cm (default): bones, meshes and keys are written in centimetres so Unreal gets a root bone with scale 1 (verified in UE 5.8). m: plain metres export (Unreal then puts a x100 scale on the root bone).")] string? units = null,
        [Description("FBX apply-scale option (default FBX_SCALE_NONE).")] string? scaleMode = null,
        bool? triangulate = null,
        [Description("Export even if validate_rig reports errors.")] bool? force = null,
        CancellationToken ct = default)
        => Results.Text(await blender.CallAsync("export_animation_for_unreal", new Args
        {
            ["armature"] = armature, ["asset_name"] = assetName, ["folder"] = folder, ["actions"] = actions,
            ["include_mesh"] = includeMesh, ["deform_only"] = deformOnly, ["unreal_folder"] = unrealFolder,
            ["target_fps"] = targetFps, ["units"] = units, ["scale_mode"] = scaleMode, ["triangulate"] = triangulate, ["force"] = force,
        }, TimeSpan.FromMinutes(10), ct));

    [McpServerTool(Name = "import_animation", Title = "Import FBX animation")]
    [Description("Import animation from an FBX (e.g. exported from Unreal or a library). End/leaf helper bones are removed, clips start at frame 1 and names are cleaned ('Armature|Walk' -> 'Walk'). With target=<armature> each clip is baked onto that rig by matching bone names and copying the world-space pose every frame (so different bone rolls and cm/m units don't matter); the imported objects are then removed.")]
    public static async Task<string> ImportAnimation(
        BlenderConnection blender,
        [Description("FBX file path.")] string path,
        [Description("Existing armature to receive the clips.")] string? target = null,
        [Description("Keep the imported armature/meshes when using target.")] bool? keepImported = null,
        [Description("Prefix for imported action names.")] string? prefix = null,
        bool? rename = null,
        double? scale = null,
        [Description("Guess bone orientation (for files from other tools; leave off for Unreal/Blender round trips).")] bool? automaticBoneOrientation = null,
        [Description("Frame each clip starts on (default 1).")] double? startFrame = null,
        CancellationToken ct = default)
        => Results.Text(await blender.CallAsync("import_animation", new Args
        {
            ["path"] = path, ["target"] = target, ["keep_imported"] = keepImported, ["prefix"] = prefix, ["rename"] = rename,
            ["scale"] = scale, ["automatic_bone_orientation"] = automaticBoneOrientation, ["start_frame"] = startFrame,
        }, TimeSpan.FromMinutes(5), ct));
}
