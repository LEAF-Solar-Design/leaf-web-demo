namespace LeafSolarDesign.ScriptHost
{
    public interface IToolLog
    {
        void Info(string msg);
        void Warn(string msg);
        void Error(string msg);
    }
}
